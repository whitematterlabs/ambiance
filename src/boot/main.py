"""The kernel loop — tickless, event + timer driven.

Sleeps on whichever fires first: an FS event in $PAI_ROOT/run/pai/events/ or the next
pending timer. When the heap is empty and no events are pending, blocks
indefinitely on the watcher.
"""

from __future__ import annotations

import asyncio
import fnmatch
import os
import signal
import subprocess
import sys
import time
import traceback
from collections import defaultdict
from datetime import date, datetime

from contextlib import AsyncExitStack
from pathlib import Path

import yaml

from . import config as C
from . import doc_watcher
from . import driver_health
from . import inject
from . import litellm_proxy
from . import outbound_echo
from . import paths
from . import processes as P
from . import proc_watcher
from . import shell_tool
from . import supervisor
from . import timers as T
from .events import EventWatcher, read_event
from .nudge import nudge
from .routing import route_to_pids as _route_to_pids


# Active nudge tasks per PAI slug — populated by _dispatch_nudge, consumed
# by 'interrupt' events (ESC from the TUI). One PAI may have multiple
# in-flight nudges if events arrive faster than they resolve; interrupt
# cancels all of them so the next owner message starts clean.
_active_nudges: dict[int, set[asyncio.Task]] = defaultdict(set)

# Per-PAI lock so concurrent nudges don't race on messages.jsonl
# (load → mutate → save). Cancellation propagates through acquire()
# cleanly — a task waiting on the lock will just raise CancelledError.
_pai_locks: dict[int, asyncio.Lock] = {}

_contacts_module = None
_messages_module = None


def _contacts_driver():
    global _contacts_module
    if _contacts_module is None:
        from drivers import contacts

        _contacts_module = contacts
    return _contacts_module


def _messages_driver():
    global _messages_module
    if _messages_module is None:
        from drivers import messages

        _messages_module = messages
    return _messages_module


def _pai_lock(to: int) -> asyncio.Lock:
    lock = _pai_locks.get(to)
    if lock is None:
        lock = asyncio.Lock()
        _pai_locks[to] = lock
    return lock


def _dispatch_nudge(
    to: int, *args, from_: int | None = None, **kwargs
) -> asyncio.Task:
    """Fire a nudge as a background task, serialized per PAI, cancellable.

    `to` is the target PAI's integer PID. `from_` is the sender's PID
    (None = kernel/system)."""
    to = int(to)
    sender = int(from_) if from_ is not None else None

    async def _run() -> None:
        async with _pai_lock(to):
            await nudge(*args, to=to, from_=sender, **kwargs)

    task = asyncio.create_task(_run(), name=f"nudge-{to}")
    _active_nudges[to].add(task)
    task.add_done_callback(lambda t: _active_nudges[to].discard(t))
    return task


def _deliver_message(
    to: int,
    reason: str,
    *,
    from_: int | None = None,
    from_kind: str = "pai",
    slug: str | None = None,
    context: dict | None = None,
    msg_id: str | None = None,
    event: dict | None = None,
) -> None:
    """Deliver a message-shaped event to a PAI.

    If the target has a turn running, the message is injected into that
    turn at its next tool boundary (see boot.inject) — it interrupts the
    ongoing work as fresh input and the turn keeps going. Otherwise it
    falls back to a queued nudge, which starts a new turn immediately
    (the slug lock is free when no turn is live).

    `event` is the originating event payload: if the turn ends before the
    injection is drained, nudge re-emits it so the message re-routes
    instead of vanishing.
    """
    to = int(to)
    try:
        target_slug = P.find_pai_slug(to)
    except P.ProcessNotFound:
        target_slug = None
    sender = f"{from_kind}:{from_}" if from_ is not None else None
    if target_slug is not None and inject.try_inject(
        target_slug, reason, slug=slug, context=context, sender=sender, event=event
    ):
        print(f"[kernel] inject: {reason} → {target_slug} (mid-turn)", flush=True)
        try:
            P.append_log(target_slug, f"injected mid-turn: {reason}")
        except P.ProcessNotFound:
            pass
        if msg_id:
            P.emit_ack(msg_id, {
                "kind": "pai_message:ack",
                "msg_id": msg_id,
                "target_pid": to,
                "slug": target_slug,
                "delivery": "injected",
            })
        return
    args = (reason, slug) if slug is not None else (reason,)
    kwargs: dict = {}
    if context is not None:
        kwargs["context"] = context
    if from_kind != "pai":
        kwargs["from_kind"] = from_kind
    if msg_id is not None:
        kwargs["msg_id"] = msg_id
    if from_ is not None:
        kwargs["from_"] = from_
    _dispatch_nudge(to, *args, **kwargs)


async def _handle_timer(entry: T.TimerEntry, heap: list[T.TimerEntry]) -> None:
    slug = entry.slug
    try:
        status = P.read_status(slug)
        spec = P.read_spec(slug)
    except P.ProcessNotFound:
        return

    if status not in P.ACTIVE_STATUSES:
        return  # stale timer; process was resolved (an armed cron is `scheduled`)

    pai = int(spec.get("parent", 1))
    schedule = spec.get("schedule")
    has_run = "run" in spec

    if schedule is not None:
        next_fire, recurring = T.parse_schedule(schedule, datetime.now())

        if recurring:
            # Cron tick — fire the run (transient subprocess) or nudge PAI.
            if has_run:
                await supervisor.fire_once(slug, spec)
            else:
                _dispatch_nudge(pai, "schedule fired", slug, spec)
            if next_fire is not None:
                T.push(heap, next_fire, slug)
                P.append_log(
                    slug,
                    f"kernel: next fire at {next_fire.isoformat(timespec='seconds')}",
                )
            return

        # One-shot schedule fired.
        if has_run:
            # Deferred background service — start under supervision; its
            # exit will resolve the proc through the normal path.
            await supervisor.start(slug, spec)
        else:
            # One-shot reminder — nudge PAI and resolve.
            _dispatch_nudge(pai, "schedule fired", slug, spec)
            P.resolve(slug, "completed")
        return

    # No schedule → deadline path. Auto-expire.
    _dispatch_nudge(pai, "deadline reached", slug, spec)
    try:
        P.resolve(slug, "expired")
    except P.ProcessNotFound:
        pass


async def _drain_elapsed_timers(heap: list[T.TimerEntry], now: datetime) -> None:
    while True:
        nxt = T.peek(heap)
        if nxt is None or nxt.fire_time > now:
            return
        entry = T.pop(heap)
        try:
            await _handle_timer(entry, heap)
        except Exception as exc:
            # A broken spec must not take down the supervisor. Mark the
            # proc failed (best-effort) and keep draining.
            try:
                P.append_log(entry.slug, f"kernel: timer handler failed: {exc!r}")
                P.resolve(entry.slug, "failed")
            except P.ProcessNotFound:
                pass


async def _safe_handle_event_file(path: Path, heap: list[T.TimerEntry]) -> None:
    """Backstop around _handle_event_file.

    A malformed event is already quarantined in read_event, but any
    downstream bug in event handling must not take down the supervisor
    (PID 1) either — symmetric with the timer-handler guard above. Control
    flow (_RestartRequested, CancelledError) is BaseException, so it still
    propagates through this `except Exception`.
    """
    try:
        await _handle_event_file(path, heap)
    except Exception as exc:
        print(
            f"[kernel] event handler failed for {path.name}: {exc!r}; continuing",
            flush=True,
        )


async def _handle_event_file(path: Path, heap: list[T.TimerEntry]) -> None:
    event = read_event(path)
    if event is None:
        return
    kind = event.get("kind")

    if kind == "interrupt":
        pai = int(event.get("pai", 1))
        tasks = list(_active_nudges.get(pai, ()))
        # An interrupt must reach the work this PAI delegated. Ad-hoc subagents
        # run as their own procs with their own nudge tasks, so cancelling the
        # parent alone leaves them running (the 2026-07-03 "LinkedIn kept trying
        # after I stopped PAI" bug). Cascade first, then cancel the parent.
        stopped = _cascade_stop_subagents(pai)
        if not tasks and not stopped:
            print(f"[kernel] interrupt: no active nudge for pai={pai}", flush=True)
            return
        suffix = f" + stopped {stopped} subagent(s)" if stopped else ""
        print(
            f"[kernel] interrupt: cancelling {len(tasks)} nudge(s) for pai={pai}{suffix}",
            flush=True,
        )
        for t in tasks:
            t.cancel()
        return

    # The `new_message`, `messages_backlog`, and `messages_multiple` branches
    # below are iMessage-shaped (require `handle`, ingest via messages.ingest,
    # as imessage:*). Other drivers emit their own `<driver>:<kind>` and fall
    # through to the generic router at the bottom. Gate on source so we don't
    # grab whatsapp/etc.
    event_source = event.get("source")
    is_imessage = event_source in (None, "imessage")

    # TUI owner messages share `kind="new_message"` but carry `source="tui"`
    # and a `target_pid`. They're not iMessage-shaped (no handle/ingest), so
    # handle them before the iMessage gate or they fall through to the
    # generic router and land on the fallback PAI instead of the targeted one.
    if (
        kind == "new_message"
        and event.get("thread") == "me"
        and isinstance(event.get("target_pid"), int)
    ):
        text = event.get("text") or ""
        if not text:
            print(f"[kernel] dropping empty owner message: {event!r}", flush=True)
            return
        pid = int(event["target_pid"])
        day = date.today().isoformat()
        ctx = {
            "thread": "me",
            "sender": "me",
            "text": text,
            "day_file": f"communication/messages/me/{P.slug_for_pid(pid)}/{day}.md",
        }
        if event.get("overclock") is True:
            ctx["overclock"] = True
        _deliver_message(
            pid,
            "owner message",
            context=ctx,
            event=event,
        )
        return

    if kind == "new_message" and is_imessage:
        M = _messages_driver()
        # TUI-originated owner messages: the line is already appended to
        # me/YYYY-MM-DD.md by the client, so we skip ingest() and just nudge.
        if event.get("thread") == "me":
            text = event.get("text") or ""
            if not text:
                print(f"[kernel] dropping empty owner message: {event!r}", flush=True)
                return
            day = date.today().isoformat()
            target = event.get("target_pid")
            if isinstance(target, int):
                pids = [target]
            else:
                pids = _route_to_pids("imessage:owner")
            for pid in pids:
                ctx = {
                    "thread": "me",
                    "sender": "me",
                    "text": text,
                    "day_file": f"communication/messages/me/{P.slug_for_pid(pid)}/{day}.md",
                }
                if event.get("overclock") is True:
                    ctx["overclock"] = True
                # Re-emit payload is pinned to this pid: the original event
                # may fan out to several PAIs, and re-processing it whole
                # would double-deliver to the ones that already got it.
                _deliver_message(
                    pid,
                    "owner message",
                    context=ctx,
                    event={**event, "target_pid": pid},
                )
            return

        handle = event.get("handle") or ""
        text = event.get("text") or ""
        if not handle or not text:
            print(f"[kernel] dropping malformed new_message event: {event!r}", flush=True)
            return
        received_at = None
        raw_ts = event.get("received_at")
        if isinstance(raw_ts, str):
            try:
                received_at = datetime.fromisoformat(raw_ts)
            except ValueError:
                received_at = None
        from_me = bool(event.get("is_from_me"))
        if from_me:
            # chat.db is reflecting a send back at us. If PAI drafted it,
            # outbound._append_canonical already wrote the line — drop the
            # echo. Otherwise it's the owner texting from their phone/Mac and
            # we need to log it as `me:` and nudge.
            existing_slug = M.resolve_slug(handle, event.get("chat_guid"))
            if existing_slug and outbound_echo.consume(existing_slug, text):
                print(
                    f"[kernel] dropped chat.db echo of PAI send → {existing_slug}",
                    flush=True,
                )
                return
        result = M.ingest(
            handle=handle,
            text=text,
            chat_guid=event.get("chat_guid"),
            display_name=event.get("display_name"),
            received_at=received_at,
            source=event.get("source"),
            sender_override="me" if from_me else None,
            chat_handles=event.get("chat_handles"),
        )
        tag = "new message"
        if from_me:
            tag = "outbound message"
        if result.created_thread:
            tag += " (new thread)"
        ctx = {
            "thread": result.slug,
            "sender": result.sender,
            "text": text,
            "day_file": f"communication/messages/{result.day_file.relative_to(paths.var_spool_messages())}",
        }
        for pid in _route_to_pids("imessage:new"):
            _dispatch_nudge(pid, tag, context=ctx)

    elif kind == "messages_backlog" and is_imessage:
        M = _messages_driver()
        messages = event.get("messages") or []
        if not messages:
            return
        # Aggregate per thread: counts of inbound vs outbound (from-me) so
        # the nudge can render "While you were offline: X sent to you, Y
        # sent from your phone."
        from collections import defaultdict
        per_thread: dict[str, dict] = defaultdict(
            lambda: {"inbound": 0, "outbound": 0, "last_text": ""}
        )
        earliest_ts: datetime | None = None
        for m in messages:
            handle = m.get("handle") or ""
            text = m.get("text") or ""
            if not handle or not text:
                continue
            from_me = bool(m.get("is_from_me"))
            recv = m.get("received_at")
            ts = None
            if isinstance(recv, str):
                try:
                    ts = datetime.fromisoformat(recv)
                except ValueError:
                    ts = None
            if ts and (earliest_ts is None or ts < earliest_ts):
                earliest_ts = ts
            result = M.ingest(
                handle=handle,
                text=text,
                chat_guid=m.get("chat_guid"),
                received_at=ts,
                source=event.get("source"),
                sender_override="me" if from_me else None,
                chat_handles=m.get("chat_handles"),
            )
            bucket = per_thread[result.slug]
            if from_me:
                bucket["outbound"] += 1
            else:
                bucket["inbound"] += 1
            bucket["last_text"] = text

        summary = [
            {
                "thread": slug,
                "inbound": b["inbound"],
                "outbound": b["outbound"],
                "last_text": b["last_text"],
            }
            for slug, b in per_thread.items()
        ]
        since = earliest_ts.isoformat(timespec="seconds") if earliest_ts else None
        ctx = {
            "since": since,
            "threads": summary,
            "total": sum(b["inbound"] + b["outbound"] for b in per_thread.values()),
        }
        for pid in _route_to_pids("imessage:backlog"):
            _dispatch_nudge(pid, "messages backlog", context=ctx)

    elif kind == "messages_multiple" and is_imessage:
        M = _messages_driver()
        # Live burst — multiple new rows in a single drain. Ingest each so
        # the day-files get written, dedupe PAI's own outbound echoes, then
        # nudge once with the full ordered list of what landed.
        messages = event.get("messages") or []
        if not messages:
            return
        ingested: list[dict] = []
        for m in messages:
            handle = m.get("handle") or ""
            text = m.get("text") or ""
            if not handle or not text:
                continue
            from_me = bool(m.get("is_from_me"))
            if from_me:
                existing_slug = M.resolve_slug(handle, m.get("chat_guid"))
                if existing_slug and outbound_echo.consume(existing_slug, text):
                    print(
                        f"[kernel] dropped chat.db echo of PAI send → {existing_slug}",
                        flush=True,
                    )
                    continue
            recv = m.get("received_at")
            ts = None
            if isinstance(recv, str):
                try:
                    ts = datetime.fromisoformat(recv)
                except ValueError:
                    ts = None
            result = M.ingest(
                handle=handle,
                text=text,
                chat_guid=m.get("chat_guid"),
                received_at=ts,
                source=event.get("source"),
                sender_override="me" if from_me else None,
                chat_handles=m.get("chat_handles"),
            )
            ingested.append({
                "thread": result.slug,
                "sender": result.sender,
                "text": text,
                "day_file": f"communication/messages/{result.day_file.relative_to(paths.var_spool_messages())}",
            })
        if not ingested:
            return  # everything was an echo
        ctx = {"messages": ingested, "total": len(ingested)}
        for pid in _route_to_pids("imessage:multiple_messages"):
            _dispatch_nudge(pid, f"{len(ingested)} new messages", context=ctx)

    elif kind == "proc_resolved":
        slug = event.get("slug")
        status = event.get("status")
        if not slug:
            print(f"[kernel] dropping malformed proc_resolved event: {event!r}", flush=True)
            return
        parent = event.get("parent")
        if parent is not None:
            # Explicit parent: notify it of any outcome (subagent return path).
            _dispatch_nudge(int(parent), f"proc {status}", slug=slug, context={"status": status})
        elif status in ("failed", "expired"):
            # No parent: only wake kernel_manager for self-healing on failures.
            _dispatch_nudge(1, f"proc {status}", slug=slug, context={"status": status})

    elif kind == "pai_message":
        target_pid = event.get("target_pid")
        text = event.get("text") or ""
        sender_pid = event.get("sender_pid")
        msg_id = event.get("msg_id")
        if target_pid is None:
            print(f"[kernel] dropping malformed pai_message event: {event!r}", flush=True)
            return
        _deliver_message(
            int(target_pid),
            "peer message",
            from_=int(sender_pid) if sender_pid is not None else None,
            context={"text": text},
            msg_id=msg_id,
            event=event,
        )

    elif kind == "subagent:response":
        target_pid = event.get("target_pid")
        text = event.get("text") or ""
        sender_pid = event.get("sender_pid")
        if target_pid is None:
            print(f"[kernel] dropping malformed subagent:response event: {event!r}", flush=True)
            return
        context = {"text": text}
        if "done" in event:
            context["done"] = bool(event.get("done"))
        if event.get("result"):
            context["result"] = event.get("result")
        if event.get("auto_fallback") is True:
            context["auto_fallback"] = True
        _deliver_message(
            int(target_pid),
            "subagent response",
            from_=int(sender_pid) if sender_pid is not None else None,
            from_kind="subagent",
            context=context,
            event=event,
        )

    elif kind in ("subagent:plan_ready", "subagent:plan_reject"):
        target_pid = event.get("target_pid")
        slug = event.get("slug") or ""
        sender_pid = event.get("sender_pid")
        if target_pid is None:
            print(f"[kernel] dropping malformed {kind} event: {event!r}", flush=True)
            return
        tag = "subagent plan ready" if kind == "subagent:plan_ready" else "subagent plan reject"
        _deliver_message(
            int(target_pid),
            tag,
            from_=int(sender_pid) if sender_pid is not None else None,
            from_kind="subagent",
            slug=slug,
            context={"slug": slug, "text": event.get("text") or ""},
            event=event,
        )

    elif kind == "send_failed" and event_source in (None, "imessage", "imessage-out"):
        # This hardcoded branch is imessage-specific: it maps the driver's
        # bare `send_failed` onto the public `imessage:send_failed` kind PAIs
        # wake on. Gate it on the imessage source so other channels that emit
        # a bare `send_failed` (e.g. whatsapp-out) fall through to the generic
        # `<source>:<kind>` router below and route as `whatsapp-out:send_failed`
        # rather than being misdelivered to imessage listeners.
        thread = event.get("thread")
        text = event.get("text") or ""
        reason = event.get("reason") or ""
        if not thread:
            print(f"[kernel] dropping malformed send_failed event: {event!r}", flush=True)
            return
        ctx = {"thread": thread, "text": text, "reason": reason}
        for pid in _route_to_pids("imessage:send_failed"):
            _dispatch_nudge(pid, "send failed", context=ctx)

    elif kind == "new_email":
        ctx = {
            "account": event.get("account"),
            "thread_slug": event.get("thread_slug"),
            "subject": event.get("subject"),
            "from": event.get("from"),
            "direction": event.get("direction"),
            "path": event.get("path"),
        }
        tag = "new email" if event.get("direction") == "inbound" else "outbound email"
        for pid in _route_to_pids("email:new"):
            _dispatch_nudge(pid, tag, context=ctx)

    elif kind == "email_backlog":
        ctx = {
            "since": event.get("since"),
            "accounts": event.get("accounts") or [],
            "total": int(event.get("total") or 0),
        }
        for pid in _route_to_pids("email:backlog"):
            _dispatch_nudge(pid, "email backlog", context=ctx)

    elif kind == "draft_failed":
        ctx = {
            "account": event.get("account"),
            "path": event.get("path"),
            "reason": event.get("reason"),
        }
        for pid in _route_to_pids("email:draft_failed"):
            _dispatch_nudge(pid, "draft failed", context=ctx)

    elif kind == "kernel:backfill":
        # Synthetic event emitted by boot.phases.backfill when the spool is
        # storming. Routes directly to target_pid in the payload — no
        # wake_on globbing — so the chosen PAI gets exactly one nudge with
        # counts and a manifest_glob it can drill into.
        target_pid = event.get("target_pid")
        if not isinstance(target_pid, int):
            print(f"[kernel] dropping malformed kernel:backfill event: {event!r}", flush=True)
            return
        _dispatch_nudge(target_pid, "backfill", context=event)

    elif kind == "kernel:reload_config":
        await _handle_reload_config(event)

    elif kind == "kernel:restart":
        await _handle_restart()

    elif kind == "cron_fired":
        slug = event.get("slug")
        rc = event.get("rc")
        if not slug:
            print(f"[kernel] dropping malformed cron_fired event: {event!r}", flush=True)
            return
        pai = int(event.get("parent", 1))
        _dispatch_nudge(pai, f"cron fired (rc={rc})", slug=slug, context={"rc": rc})

    else:
        # `pai:<slug>:input` / `pai:<slug>:output` are announcement events
        # emitted by every PAI turn (see nudge.py). They are meant for
        # listeners with an explicit `wake_on` match and must NOT fall
        # back to root — otherwise root self-nudges on its own turn and
        # the trigger payload (which can contain the originating message
        # text in full) snowballs into an infinite loop.
        if isinstance(kind, str) and kind.startswith("pai:"):
            matched: list[int] = []
            for slug, spec in P._iter_pai_specs():
                try:
                    if P.read_status(slug) != "running":
                        continue
                except P.ProcessNotFound:
                    continue
                pid = spec.get("pid")
                if not isinstance(pid, int):
                    continue
                wake_on = spec.get("wake_on") or []
                if isinstance(wake_on, list) and any(
                    fnmatch.fnmatchcase(kind, pat) for pat in wake_on
                ):
                    matched.append(pid)
            for pid in sorted(matched):
                _dispatch_nudge(pid, f"event: {kind}", context=event)
            return

        # Generic driver event routing: an event with `source: <driver>` and
        # `kind: <bare>` becomes the public kind `<driver>:<bare>` and is
        # routed by `_route_to_pids` (wake_on match → fan-out, else fallback).
        # This is what lets new drivers route their events without a
        # kernel patch — the kernel doesn't know what `voice:utterance` or
        # any future driver kind means; it just matches and forwards.
        source = event.get("source")
        if isinstance(source, str) and source and source != "kernel" and isinstance(kind, str) and kind:
            public_kind = f"{source}:{kind}"
            # `target_pid`, if set by the emitting driver, bypasses wake_on
            # and delivers only to that pid. Used by drivers that own per-PAI
            # session state (e.g. ax) and don't want to fan out by kind.
            tp = event.get("target_pid")
            target_pid = tp if isinstance(tp, int) else None
            for pid in _route_to_pids(public_kind, target_pid=target_pid):
                _dispatch_nudge(pid, f"event: {public_kind}", context=event)
            return

        pai = int(event.get("parent", 1))
        _dispatch_nudge(pai, f"event: {kind or 'unknown'}", context=event)


class _Tee:
    """Write to both the real stream and a file. Mirrors stdout to disk so
    clients (e.g. the TUI) can tail kernel output as a plain file."""

    def __init__(self, stream, file):
        self._stream = stream
        self._file = file

    def write(self, data: str) -> int:
        self._stream.write(data)
        try:
            self._file.write(data)
            self._file.flush()
        except Exception:
            pass
        return len(data)

    def flush(self) -> None:
        self._stream.flush()
        try:
            self._file.flush()
        except Exception:
            pass

    def __getattr__(self, name: str):
        return getattr(self._stream, name)


def _install_stdout_tee() -> None:
    # The kernel always tees stdout/stderr into kernel.log — this is the
    # always-on daemon's only log, so it must work regardless of how the
    # kernel was started (TTY shell, backgrounded, PAI.app, …).
    #
    # The one case to avoid is double-writing: a caller (e.g. PAI.app's
    # KernelLauncher) may have already pointed our stdout *straight at*
    # kernel.log. Tee-ing there would duplicate every line. Detect it by
    # comparing inodes and bail if stdout already IS the log file.
    log_path = paths.var_log() / "kernel" / "kernel.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        cur = os.fstat(sys.stdout.fileno())
        if log_path.exists():
            tgt = log_path.stat()
            if cur.st_dev == tgt.st_dev and cur.st_ino == tgt.st_ino:
                return
    except (AttributeError, ValueError, OSError):
        pass
    f = log_path.open("a", buffering=1, encoding="utf-8")
    sys.stdout = _Tee(sys.stdout, f)
    sys.stderr = _Tee(sys.stderr, f)



class _RestartRequested(BaseException):
    """Signal from the kernel:restart handler. Bubbles through run()'s
    finally block so the existing shutdown sequence runs, then is caught
    by entry.py which re-execs the kernel in place."""


# Set by _handle_restart() before raising. entry.py reads this after
# asyncio.run() returns to decide whether to execvp.
_restart_requested: bool = False


_RESTART_DRAIN_TIMEOUT = 5.0
# reload_config drains the same per-PAI locks, but unlike restart it must NOT
# block the kernel's single-threaded event loop on a busy PAI: a mid-turn PAI
# (e.g. one running a long install in its bash tool) holds its lock for minutes,
# and the main loop awaits one event handler before consuming the next, so an
# unbounded drain starves every queued event behind it (the 2026-06-18 wedge).
_RELOAD_DRAIN_TIMEOUT = 5.0


def _is_ad_hoc_subagent_spec(spec: dict) -> bool:
    """A parent-owned one-shot PAI child, not a declared service."""
    return (
        spec.get("kind") == "pai"
        and "parent" in spec
        and "run" not in spec
        and "schedule" not in spec
    )


def _cascade_stop_subagents(parent_pid: int) -> int:
    """Stop every ad-hoc subagent under `parent_pid`, depth-first. Returns count.

    Called when the owner interrupts a PAI: the interrupt cancels the parent's
    own nudge task, and this reaches the children that PAI delegated work to.
    For each ad-hoc child we cancel its in-flight nudge task (so it can't finish
    the current turn) and resolve its proc to `stopped` (so it can't start
    another). Grandchildren are stopped first so a dying child can't be seen as
    a live parent. `notify_parent=False`: the parent is being interrupted right
    now, so a "child stopped" nudge would be noise (and could re-wake it)."""
    stopped = 0
    for slug, child_pid in P.ad_hoc_children(parent_pid):
        stopped += _cascade_stop_subagents(child_pid)
        for t in list(_active_nudges.get(child_pid, ())):
            t.cancel()
        try:
            P.append_log(slug, "kernel: stopped by owner interrupt of parent")
            P.resolve(slug, "stopped", notify_parent=False)
        except P.ProcessNotFound:
            continue
        except Exception as e:
            print(
                f"[kernel] interrupt: failed to stop subagent {slug}: {e!r}",
                flush=True,
            )
            continue
        stopped += 1
    return stopped


async def _drain_pai_locks() -> None:
    async with AsyncExitStack() as stack:
        for lock in list(_pai_locks.values()):
            await stack.enter_async_context(lock)


async def _handle_restart() -> None:
    """Drain in-flight nudges with a bounded timeout, then restart.

    Drain is best-effort. A runaway driver (or any source generating
    nudges faster than they complete) can hold per-PAI locks indefinitely
    — we must not block `reboot` on the very thing the operator is trying
    to restart away from. After RESTART_DRAIN_TIMEOUT we proceed regardless;
    any in-flight nudges get cancelled at process exit, which is what
    a restart implies anyway.
    """
    global _restart_requested
    print("[kernel] restart: draining nudges", flush=True)
    try:
        await asyncio.wait_for(_drain_pai_locks(), timeout=_RESTART_DRAIN_TIMEOUT)
    except asyncio.TimeoutError:
        print(
            f"[kernel] restart: drain timed out after {_RESTART_DRAIN_TIMEOUT}s; "
            f"restarting anyway (in-flight nudges will be cancelled)",
            flush=True,
        )
    print("[kernel] restart: triggering shutdown", flush=True)
    _restart_requested = True
    raise _RestartRequested()


async def _handle_reload_config(event: dict | None = None) -> None:
    """Reconcile drivers promptly, then drain nudges + reconcile PAIs.

    Driver reconcile runs *before* draining per-PAI locks: a runaway driver
    can be generating nudges faster than they drain (every keystroke wakes
    PAI), so waiting on the drain to cancel the driver is a feedback loop
    that takes seconds-to-minutes. PAI/config reconcile still drains first
    because it mutates spec.yaml that in-flight nudges read.

    Shielded against cancellation so a SIGTERM mid-reload doesn't leave the
    driver registry inconsistent with /proc — otherwise the next boot sees
    stale `running`/`cancelled` status that blocks respawn.
    """
    # Attribute the reload: every emitter (paictl/paiman/paiadd/paidel/web/
    # paisetup) stamps a `source` and often an `action`/`name`. Logging it is
    # the only way to tell a single intentional reload from a back-to-back
    # storm after the fact — the event files are deleted once consumed.
    event = event or {}
    source = event.get("source") or "unknown"
    extra = {k: v for k, v in event.items() if k not in ("kind", "source")}
    detail = f" {extra}" if extra else ""
    print(f"[kernel] reload_config: requested by {source}{detail}", flush=True)

    try:
        await asyncio.shield(_reconcile_drivers())
    except Exception as e:
        tb = traceback.format_exc()
        print(f"[kernel] reload_config: driver reconcile failed: {e!r}\n{tb}", flush=True)

    print("[kernel] reload_config: draining nudges", flush=True)
    async with AsyncExitStack() as stack:
        async def _acquire_locks() -> None:
            for lock in list(_pai_locks.values()):
                await stack.enter_async_context(lock)

        # Best-effort, bounded drain. On timeout we proceed holding whatever
        # subset we acquired (the cancelled acquire never enters the stack, so
        # nothing leaks); the stack releases them all at scope exit. A busy PAI
        # must never wedge the loop — mirrors _handle_restart's drain.
        try:
            await asyncio.wait_for(_acquire_locks(), timeout=_RELOAD_DRAIN_TIMEOUT)
        except asyncio.TimeoutError:
            print(
                f"[kernel] reload_config: drain timed out after "
                f"{_RELOAD_DRAIN_TIMEOUT}s; reloading with best-effort locks "
                f"(a busy PAI must not starve queued events)",
                flush=True,
            )
        try:
            # Keys entered from the web console land in $PAI_ROOT/.env after
            # boot already snapshotted the env; re-read them and rebuild the
            # per-provider clients (they capture the key at construction).
            import boot as _boot
            from . import llm as _llm
            _boot.reload_env()
            _llm._clients.clear()

            C.reconcile_from_config()
            # Re-stitch every running PAI's home view so newly-installed
            # skills/prompts surface without a reboot. Mirrors the boot-time
            # loop in phases/reconcile.py — idempotent, heals broken links.
            from . import stitch
            for slug in C.load_config():
                try:
                    stitch.stitch_home(slug)
                except Exception as e:
                    print(f"[kernel] reload_config: stitch {slug} failed: {e!r}", flush=True)
            # Start/stop the LiteLLM proxy if adding/removing a proxied PAI
            # changed whether the fleet needs it — no reboot required. The
            # event rides along so a config change (new proxied provider) or a
            # set-api-key for a proxied provider restarts a running proxy,
            # whose config and env are otherwise frozen at spawn.
            await litellm_proxy.reconcile(event)
            print("[kernel] reload_config: done", flush=True)
        except Exception as e:
            tb = traceback.format_exc()
            print(f"[kernel] reload_config failed: {e!r}\n{tb}", flush=True)
            ctx = {"error": repr(e), "traceback": tb}
            for pid in _route_to_pids("kernel:reload_failed"):
                _dispatch_nudge(pid, "config reload failed", context=ctx)


# Kernel-owned driver registry. The slug is also the /proc/<slug>/ name.
# `active:` in /proc/<slug>/spec.yaml (default true) decides whether the
# coroutine is currently running; paictl flips it and emits
# kernel:reload_config to trigger _reconcile_drivers.
#
# Built dynamically by walking /usr/lib/drivers/<name>/events.yaml for a
# `processes:` section. Drivers without runnable processes (libraries
# like contacts/messages) are skipped. Discovery is re-run on every
# reconcile (boot + each kernel:reload_config), so `paiman install`/
# `remove` of a driver takes effect live — no kernel re-exec.
def _make_factory(module_path: str, attr: str):
    def factory():
        import importlib
        mod = importlib.import_module(module_path)
        return getattr(mod, attr)()
    return factory


# Per-slug default for the /proc `active:` flag, from the manifest process
# entry's `default_active:` (absent → true). Lets a privacy-sensitive driver
# (e.g. the voice host-mic listener) install dark: its proc entry is created
# stopped and nothing runs until paictl/the web console flips it on.
_driver_default_active: dict[str, bool] = {}

# Per-slug manifest identity (module, entrypoint) as of the last discovery.
# Compared against `_driver_started_with` in _reconcile_drivers so a
# reinstall that changes a process's module/entrypoint restarts the running
# task with the new spec instead of leaving the old coroutine live.
_driver_spec_identity: dict[str, tuple[str, str]] = {}


def _discover_driver_specs() -> tuple[tuple[str, object], ...]:
    """Walk every events.yaml under /usr/lib/drivers/ (any depth) and
    collect processes:. Sub-driver namespaces like email/macmail/ are
    supported by recursing through symlinks (paiman installs each
    driver as a symlink to /opt/paiman/<name>/).

    Re-runnable: refreshes `_driver_default_active` and
    `_driver_spec_identity` for every discovered slug and prunes entries
    whose manifests are gone (driver uninstalled)."""
    import os
    specs: list[tuple[str, object]] = []
    seen: set[str] = set()
    drivers_dir = paths.usr_lib_drivers()
    found: list[Path] = []
    if drivers_dir.is_dir():
        for root, _dirs, files in os.walk(drivers_dir, followlinks=True):
            if "events.yaml" in files:
                found.append(Path(root) / "events.yaml")
    for events_path in sorted(found):
        try:
            with events_path.open() as f:
                manifest = yaml.safe_load(f) or {}
        except Exception as e:
            rel = events_path.relative_to(drivers_dir)
            print(
                f"[kernel] driver {rel}: events.yaml unreadable ({e!r})",
                flush=True,
            )
            continue
        for proc in manifest.get("processes") or []:
            slug = proc["slug"]
            module = proc["module"]
            entrypoint = proc.get("entrypoint", "run")
            seen.add(slug)
            _driver_default_active[slug] = proc.get("default_active", True) is not False
            _driver_spec_identity[slug] = (module, entrypoint)
            specs.append((slug, _make_factory(module, entrypoint)))
    for stale in set(_driver_default_active) - seen:
        del _driver_default_active[stale]
    for stale in set(_driver_spec_identity) - seen:
        del _driver_spec_identity[stale]
    return tuple(specs)


# Boot-time snapshot, kept for import compatibility. The live registry is
# whatever _discover_driver_specs() returns *now* — _reconcile_drivers
# re-discovers on every call, so don't read this for current state.
DRIVER_SPECS: tuple[tuple[str, object], ...] = _discover_driver_specs()

_driver_tasks: dict[str, asyncio.Task] = {}

# (module, entrypoint) each running task in _driver_tasks was started with —
# None when unknown (task predates discovery, or discovery was monkeypatched).
_driver_started_with: dict[str, tuple[str, str] | None] = {}


def _driver_active(slug: str) -> bool:
    """Read `active` from /proc/<slug>/spec.yaml. Missing proc → the
    manifest's `default_active` (absent → True)."""
    try:
        spec = P.read_spec(slug)
    except P.ProcessNotFound:
        return _driver_default_active.get(slug, True)
    val = spec.get("active", True)
    return bool(val) if isinstance(val, bool) else True


def _ensure_driver_proc(slug: str) -> None:
    """Idempotent proc entry for a long-running kernel-owned driver.

    First spawn writes `kind: driver, active: true`. On subsequent spawns
    the existing spec (including any `active:` flipped by paictl) is left
    untouched; status is unconditionally reset to `running` so a previous
    `cancelled`/`failed` resolution doesn't make /proc look terminal while
    the supervise task is in fact live again."""
    proc = P.PROC_DIR / slug
    if proc.exists():
        (proc / "status").write_text("running\n")
        try:
            P.append_log(slug, "kernel: restarted")
        except P.ProcessNotFound:
            pass
    else:
        P.spawn(slug, {"kind": "driver", "active": True})


async def _reconcile_drivers() -> None:
    """Bring running driver tasks into sync with /proc `active:` flags.

    Spawns drivers that should run but aren't, cancels drivers that are
    running but shouldn't. Idempotent. Called once at boot and on every
    `kernel:reload_config` event — never on a timer.

    Re-discovers /usr/lib/drivers/ each call so paiman install/remove takes
    effect on reload without a kernel restart: a new manifest spawns (subject
    to `default_active` / the /proc `active:` flag), a vanished manifest
    cancels the running task, and a changed spec (module/entrypoint) restarts
    the task with the new factory. Unchanged specs are left alone — no task
    churn on back-to-back reloads.

    Done-task cleanup: a driver task that exited on its own (crash, clean
    return, or cancellation that wasn't awaited here because the reload
    handler itself was cancelled) leaves a stale `.done()` entry in
    `_driver_tasks`. We GC those up-front so the `active and not running`
    branch correctly identifies the slot as free and respawns.
    """
    specs = _discover_driver_specs()
    known = {slug for slug, _ in specs}

    # GC any task whose coroutine has finished. Without this, a respawn
    # after a crash or stale cancellation can't tell "still running" from
    # "long dead" and silently no-ops.
    for slug in list(_driver_tasks):
        task = _driver_tasks[slug]
        if task.done():
            # Drain the exception (if any) so asyncio doesn't log it as
            # unretrieved later.
            try:
                task.result()
            except (asyncio.CancelledError, Exception):
                pass
            del _driver_tasks[slug]
            _driver_started_with.pop(slug, None)

    for slug, factory in specs:
        active = _driver_active(slug)
        running = slug in _driver_tasks  # already filtered to live tasks above
        if active and running:
            # Spec skew check: the task was started from an older manifest.
            # `.get(slug, want)` — an unknown started-with identity (e.g.
            # discovery monkeypatched in tests) is treated as unchanged.
            want = _driver_spec_identity.get(slug)
            have = _driver_started_with.get(slug, want)
            if want != have:
                task = _driver_tasks.pop(slug)
                _driver_started_with.pop(slug, None)
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
                print(
                    f"[kernel] driver spec changed, restarting: {slug} "
                    f"({have} → {want})",
                    flush=True,
                )
                running = False
        if active and not running:
            try:
                task = asyncio.create_task(
                    _supervise_driver(slug, factory()),
                    name=slug,
                )
            except Exception as e:
                tb = traceback.format_exc()
                print(f"[kernel] driver {slug}: failed to start — {e!r}\n{tb}", flush=True)
                _ensure_driver_proc(slug)
                driver_health.record_exit(slug, "failed_to_start", repr(e))
                try:
                    P.append_log(slug, f"failed to start: {e!r}")
                    for line in tb.rstrip().splitlines():
                        P.append_log(slug, f"  {line}")
                    P.resolve(slug, "failed")
                except P.ProcessNotFound:
                    pass
                continue
            _driver_tasks[slug] = task
            _driver_started_with[slug] = _driver_spec_identity.get(slug)
            print(f"[kernel] driver started: {slug}", flush=True)
        elif not active and not running:
            # A default-off driver that has never started still needs its
            # /proc entry: paictl start / the web toggle flip `active:`
            # there and error out on a missing proc.
            proc_dir = P.PROC_DIR / slug
            if not proc_dir.exists():
                P.spawn(slug, {"kind": "driver", "active": False})
                (proc_dir / "status").write_text("stopped\n")
        elif not active and running:
            task = _driver_tasks.pop(slug)
            _driver_started_with.pop(slug, None)
            task.cancel()
            # Drop the lock-equivalent (the dict entry) before awaiting so
            # if *this* reconcile is itself cancelled mid-await, the next
            # reconcile sees the slot empty and can respawn cleanly.
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
            print(f"[kernel] driver stopped: {slug}", flush=True)
    for slug in list(_driver_tasks):
        if slug in known or slug == "proc-watcher":
            continue
        task = _driver_tasks.pop(slug)
        _driver_started_with.pop(slug, None)
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        print(f"[kernel] driver removed: {slug}", flush=True)


async def _supervise_driver(slug: str, coro) -> None:
    """Run a driver coroutine under a proc entry.

    Resolves `cancelled` on shutdown (no nudge) and `failed` on crash
    (nudges PAI via the standard proc_resolved path).
    """
    _ensure_driver_proc(slug)
    driver_health.record_start(slug)
    try:
        await coro
        # A driver coroutine returning on its own is the quiet failure mode
        # (a long-running ingester should never finish). /proc status still
        # says "running" here, so the health breadcrumb is the only durable
        # record that the task is gone.
        driver_health.record_exit(slug, "returned")
    except asyncio.CancelledError:
        # Don't clobber a replacement's "running" status. A stop+start
        # sequence cancels this task and spawns a fresh supervise; that
        # fresh supervise has already called _ensure_driver_proc (which
        # wrote "running") by the time our cancellation cleanup runs here.
        # Writing "cancelled" now would race-overwrite the new status and
        # leave /proc looking dead while the driver is in fact live.
        current = _driver_tasks.get(slug)
        if current is None or current is asyncio.current_task():
            # Same replacement guard for the health breadcrumb: a stop+start's
            # fresh supervise has already recorded its start; writing our exit
            # here would make the panel call the live replacement dead.
            driver_health.record_exit(slug, "cancelled")
            try:
                P.resolve(slug, "cancelled")
            except P.ProcessNotFound:
                pass
        raise
    except Exception as e:
        tb = traceback.format_exc()
        driver_health.record_exit(slug, "crashed", repr(e))
        print(f"[driver:{slug}] crashed: {e}\n{tb}", flush=True)
        try:
            P.append_log(slug, f"crashed: {e!r}")
            for line in tb.rstrip().splitlines():
                P.append_log(slug, f"  {line}")
            P.resolve(slug, "failed")
        except P.ProcessNotFound:
            pass


async def run() -> None:
    _install_stdout_tee()
    loop = asyncio.get_running_loop()
    main_task = asyncio.current_task()

    def _request_shutdown(signame: str) -> None:
        print(f"[kernel] received {signame}, shutting down", flush=True)
        if main_task is not None:
            main_task.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_shutdown, sig.name)
        except NotImplementedError:
            pass

    # NOTE: layout/legacy migrations moved to boot.phases. Reconcile is
    # now phase 4 and runs before this function is invoked.
    M = _messages_driver()
    _contacts_driver().sync(M.PEOPLE_DIR, M.MESSAGES_DIR)
    heap = T.rebuild_from_proc()
    watcher = EventWatcher(P.EVENTS_DIR, loop)
    watcher.start()
    await supervisor.resume_from_disk()
    print(f"[kernel] supervise: started — {len(heap)} timers loaded", flush=True)

    proc_watcher_task = asyncio.create_task(
        _supervise_driver("proc-watcher", proc_watcher.run(heap)),
        name="proc-watcher",
    )
    doc_watcher_task = asyncio.create_task(
        _supervise_driver("doc-watcher", doc_watcher.run()),
        name="doc-watcher",
    )
    await _reconcile_drivers()
    await litellm_proxy.reconcile()

    # Fleet is up — greet the owner. Route a synthetic `online` kind through
    # the normal wake_on/fallback router. The kind is deliberately NOT under
    # `kernel:*` so root's kernel-internal glob doesn't swallow it; with no
    # PAI opting in via wake_on: ["online"], it falls through to the
    # owner-facing fallback PAI — the same one that handles owner messages.
    # Fires on every boot, including kernel:restart re-execs.
    for pid in _route_to_pids("online"):
        _dispatch_nudge(
            pid,
            "online",
            context={
                "instruction": (
                    "You are now online. Greet your owner "
                    "in one short, natural line. No status report, no tool "
                    "calls unless they ask for something."
                ),
            },
        )

    try:
        while True:
            now = datetime.now()
            await _drain_elapsed_timers(heap, now)
            timeout = T.time_until_next(heap, datetime.now())

            event_task = asyncio.create_task(watcher.next())
            if timeout is None:
                await event_task
                await _safe_handle_event_file(event_task.result(), heap)
            else:
                sleep_task = asyncio.create_task(asyncio.sleep(timeout))
                done, pending = await asyncio.wait(
                    {event_task, sleep_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for t in pending:
                    t.cancel()
                if event_task in done:
                    await _safe_handle_event_file(event_task.result(), heap)
    except asyncio.CancelledError:
        # SIGINT/SIGTERM handler cancels main_task; that's the expected
        # shutdown path, not a crash. Let `finally` run the orderly drain
        # and return normally so entry.py exits 0 instead of logging
        # "[kernel] fatal: uncaught in supervise.run()".
        pass
    except _RestartRequested:
        pass  # finally runs the shutdown; entry.py execs after run() returns
    finally:
        for tasks in _active_nudges.values():
            for t in tasks:
                t.cancel()
        await supervisor.shutdown()
        all_tasks = [proc_watcher_task, doc_watcher_task, *_driver_tasks.values()]
        for t in all_tasks:
            t.cancel()
        for t in all_tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        _driver_tasks.clear()
        _driver_started_with.clear()
        watcher.stop()
        try:
            remaining = P.list_procs(status_filter="running")
            # Armed timers already rest at `scheduled`, so they're not in the
            # `running` set and survive this sweep untouched (rebuild_from_proc
            # re-arms them on next boot). The only `running` proc that still
            # carries a `schedule:` is a one-shot service mid-fire — keep it
            # running so its restart policy applies on resume.
            survivors = []
            restart_interrupted_subagents = []
            for slug in remaining:
                try:
                    spec = P.read_spec(slug)
                except P.ProcessNotFound:
                    spec = {}
                if "schedule" in spec:
                    survivors.append(slug)
                elif _restart_requested and _is_ad_hoc_subagent_spec(spec):
                    restart_interrupted_subagents.append(slug)
            preserved = set(survivors) | set(restart_interrupted_subagents)
            to_resolve = [s for s in remaining if s not in preserved]
            if to_resolve:
                print(f"[kernel] shutdown: resolving {len(to_resolve)} procs", flush=True)
                for slug in to_resolve:
                    try:
                        P.resolve(slug, "stopped")
                    except Exception as e:
                        print(f"[kernel] failed to resolve {slug}: {e!r}", flush=True)
            if survivors:
                print(f"[kernel] shutdown: preserving {len(survivors)} cron procs across restart", flush=True)
            if restart_interrupted_subagents:
                print(
                    "[kernel] shutdown: preserving "
                    f"{len(restart_interrupted_subagents)} interrupted subagent procs "
                    "for boot-time parent notification",
                    flush=True,
                )
        except Exception as e:
            print(f"[kernel] shutdown sweep failed: {e!r}", flush=True)
        try:
            await shell_tool.shutdown_all()
        except Exception as e:
            print(f"[kernel] shell_tool shutdown failed: {e!r}", flush=True)
        try:
            run_dir = paths.PAI_ROOT / "run"
            socks = sorted(run_dir.glob("tmux-*.sock"))
            if socks:
                print(f"[kernel] shutdown: killing {len(socks)} tmux servers", flush=True)
                for sock in socks:
                    try:
                        subprocess.run(
                            ["tmux", "-S", str(sock), "kill-server"],
                            timeout=2,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                        )
                    except Exception as e:
                        print(f"[kernel] tmux kill-server failed for {sock.name}: {e!r}", flush=True)
                    try:
                        sock.unlink()
                    except FileNotFoundError:
                        pass
                    except Exception as e:
                        print(f"[kernel] unlink failed for {sock.name}: {e!r}", flush=True)
        except Exception as e:
            print(f"[kernel] tmux reap sweep failed: {e!r}", flush=True)
        _reap_pgrp()
        _reap_descendants()
        print("[kernel] stopped", flush=True)


def _reap_pgrp(grace: float = 2.0) -> None:
    """SIGTERM every other process in our process group, then SIGKILL survivors.

    Why: driver coroutines that spawn external subprocesses (chromium, tmux,
    long-running watchers) don't always tear them down cleanly when cancelled.
    The kernel is its own pgrp leader (started via shell job control or
    start_new_session), so any descendant — direct or grand- — sits in our
    pgrp and is reachable here. Without this, Ctrl-C kills the kernel but
    leaves orphaned drivers behind, which then race a fresh kernel on restart.
    """
    my_pid = os.getpid()
    try:
        pgid = os.getpgrp()
    except OSError:
        return
    # Only reap if we're the pgrp leader — otherwise we'd be signaling
    # processes we don't own (e.g. a parent shell).
    if pgid != my_pid:
        return
    try:
        ps = paths.host_executable("ps")
        if ps is None:
            return
        out = subprocess.check_output(
            [ps, "-eo", "pid=,pgid="],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        return
    targets: list[int] = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            pid, gid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        if gid == pgid and pid != my_pid:
            targets.append(pid)
    if not targets:
        return
    for pid in targets:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        targets = [p for p in targets if _pid_alive(p)]
        if not targets:
            return
        time.sleep(0.1)
    for pid in targets:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _reap_descendants(grace: float = 2.0) -> None:
    """SIGTERM every descendant of this kernel by PPID-tree, then SIGKILL survivors.

    Why: drivers spawn children that daemonize (call setsid()/setpgrp() or
    fork-and-detach) — tmux servers, node bridges, headless browsers. Those
    leave the kernel's pgrp, so _reap_pgrp() can't see them, but they remain
    descendants in the PPID tree until the kernel exits and they reparent
    to PID 1. Walking the tree here, *before* the kernel exits, catches
    them while the link is still intact.
    """
    my_pid = os.getpid()
    try:
        ps = paths.host_executable("ps")
        if ps is None:
            return
        out = subprocess.check_output(
            [ps, "-eo", "pid=,ppid="],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        return
    children: dict[int, list[int]] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        children.setdefault(ppid, []).append(pid)
    targets: list[int] = []
    stack = list(children.get(my_pid, []))
    seen: set[int] = set()
    while stack:
        pid = stack.pop()
        if pid in seen or pid == my_pid:
            continue
        seen.add(pid)
        targets.append(pid)
        stack.extend(children.get(pid, []))
    if not targets:
        return
    print(f"[kernel] shutdown: reaping {len(targets)} descendant procs", flush=True)
    for pid in targets:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except PermissionError:
            pass
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        targets = [p for p in targets if _pid_alive(p)]
        if not targets:
            return
        time.sleep(0.1)
    for pid in targets:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError:
            pass


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
