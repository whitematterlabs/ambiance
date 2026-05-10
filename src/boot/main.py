"""The kernel loop — tickless, event + timer driven.

Sleeps on whichever fires first: an FS event in home/events/ or the next
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

from drivers import contacts
from drivers import messages as M

from . import config as C
from . import outbound_echo
from . import paths
from . import processes as P
from . import proc_watcher
from . import shell_tool
from . import supervisor
from . import timers as T
from .events import EventWatcher, read_event
from .nudge import nudge


# Active nudge tasks per PAI slug — populated by _dispatch_nudge, consumed
# by 'interrupt' events (ESC from the TUI). One PAI may have multiple
# in-flight nudges if events arrive faster than they resolve; interrupt
# cancels all of them so the next owner message starts clean.
_active_nudges: dict[int, set[asyncio.Task]] = defaultdict(set)

# Per-PAI lock so concurrent nudges don't race on messages.jsonl
# (load → mutate → save). Cancellation propagates through acquire()
# cleanly — a task waiting on the lock will just raise CancelledError.
_pai_locks: dict[int, asyncio.Lock] = {}


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


def _route_to_pids(event_kind: str, fallback_pid: int = 1) -> list[int]:
    """Every running PAI that should be nudged for `event_kind`, by pid.

    Two-tier:
      1. Every PAI whose `wake_on` glob matches → nudged (fan-out).
      2. If zero PAIs matched, every PAI with `fallback: true` → nudged.
      3. If still zero, [fallback_pid] (pid 1 = kernel_manager) so the
         event always lands somewhere.
    """
    matched: list[int] = []
    fallbacks: list[int] = []
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
            fnmatch.fnmatchcase(event_kind, pat) for pat in wake_on
        ):
            matched.append(pid)
        elif spec.get("fallback") is True:
            fallbacks.append(pid)
    chosen = matched or fallbacks or [fallback_pid]
    chosen.sort()
    return chosen


async def _handle_timer(entry: T.TimerEntry, heap: list[T.TimerEntry]) -> None:
    slug = entry.slug
    try:
        status = P.read_status(slug)
        spec = P.read_spec(slug)
    except P.ProcessNotFound:
        return

    if status != "running":
        return  # stale timer; process was resolved

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
        await _handle_timer(entry, heap)


async def _handle_event_file(path: Path, heap: list[T.TimerEntry]) -> None:
    event = read_event(path)
    if event is None:
        return
    kind = event.get("kind")

    if kind == "interrupt":
        pai = int(event.get("pai", 1))
        tasks = list(_active_nudges.get(pai, ()))
        if not tasks:
            print(f"[kernel] interrupt: no active nudge for pai={pai}", flush=True)
            return
        print(
            f"[kernel] interrupt: cancelling {len(tasks)} nudge(s) for pai={pai}",
            flush=True,
        )
        for t in tasks:
            t.cancel()
        return

    # The `new_message` and `messages_backlog` branches below are iMessage-
    # shaped (require `handle`, ingest via M.ingest, route as imessage:*). Other
    # drivers emit their own `<driver>:<kind>` and fall through to the generic
    # router at the bottom. Gate on source so we don't grab whatsapp/etc.
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
        _dispatch_nudge(
            pid,
            "owner message",
            context={
                "thread": "me",
                "sender": "me",
                "text": text,
                "day_file": f"communication/messages/me/{pid}/{day}.md",
            },
        )
        return

    if kind == "new_message" and is_imessage:
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
                _dispatch_nudge(
                    pid,
                    "owner message",
                    context={
                        "thread": "me",
                        "sender": "me",
                        "text": text,
                        "day_file": f"communication/messages/me/{pid}/{day}.md",
                    },
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
        if target_pid is None:
            print(f"[kernel] dropping malformed pai_message event: {event!r}", flush=True)
            return
        _dispatch_nudge(
            int(target_pid),
            "peer message",
            from_=int(sender_pid) if sender_pid is not None else None,
            context={"text": text},
        )

    elif kind == "subagent:response":
        target_pid = event.get("target_pid")
        text = event.get("text") or ""
        sender_pid = event.get("sender_pid")
        if target_pid is None:
            print(f"[kernel] dropping malformed subagent:response event: {event!r}", flush=True)
            return
        _dispatch_nudge(
            int(target_pid),
            "subagent response",
            from_=int(sender_pid) if sender_pid is not None else None,
            from_kind="subagent",
            context={"text": text},
        )

    elif kind in ("subagent:plan_ready", "subagent:plan_reject"):
        target_pid = event.get("target_pid")
        slug = event.get("slug") or ""
        sender_pid = event.get("sender_pid")
        if target_pid is None:
            print(f"[kernel] dropping malformed {kind} event: {event!r}", flush=True)
            return
        tag = "subagent plan ready" if kind == "subagent:plan_ready" else "subagent plan reject"
        _dispatch_nudge(
            int(target_pid),
            tag,
            from_=int(sender_pid) if sender_pid is not None else None,
            from_kind="subagent",
            slug=slug,
            context={"slug": slug, "text": event.get("text") or ""},
        )

    elif kind == "send_failed":
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

    elif kind == "kernel:reload_config":
        await _handle_reload_config()

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
            for pid in _route_to_pids(public_kind):
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
    # If stdout is already redirected (e.g. by the pai.py supervisor writing
    # directly to kernel.log), the caller owns the log — don't double-write.
    try:
        if not sys.stdout.isatty():
            return
    except (AttributeError, ValueError):
        return
    log_path = P.HOME_DIR / "var" / "log" / "kernel" / "kernel.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
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


async def _handle_restart() -> None:
    """Drain in-flight nudges, then trigger a graceful self-restart."""
    global _restart_requested
    print("[kernel] restart: draining nudges", flush=True)
    async with AsyncExitStack() as stack:
        for lock in list(_pai_locks.values()):
            await stack.enter_async_context(lock)
    print("[kernel] restart: triggering shutdown", flush=True)
    _restart_requested = True
    raise _RestartRequested()


async def _handle_reload_config() -> None:
    """Drain in-flight nudges, then reconcile. On error, nudge pid 1."""
    print("[kernel] reload_config: draining nudges", flush=True)
    async with AsyncExitStack() as stack:
        for lock in list(_pai_locks.values()):
            await stack.enter_async_context(lock)
        try:
            C.reconcile_from_config()
            await _reconcile_drivers()
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
# like contacts/messages) are skipped.
def _make_factory(module_path: str, attr: str):
    def factory():
        import importlib
        mod = importlib.import_module(module_path)
        return getattr(mod, attr)()
    return factory


def _discover_driver_specs() -> tuple[tuple[str, object], ...]:
    """Walk every events.yaml under /usr/lib/drivers/ (any depth) and
    collect processes:. Sub-driver namespaces like email/macmail/ are
    supported by recursing through symlinks (paiman installs each
    driver as a symlink to /opt/paiman/<name>/)."""
    import os
    specs: list[tuple[str, object]] = []
    drivers_dir = paths.usr_lib_drivers()
    if not drivers_dir.is_dir():
        return ()
    found: list[Path] = []
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
            specs.append((slug, _make_factory(module, entrypoint)))
    return tuple(specs)


DRIVER_SPECS: tuple[tuple[str, object], ...] = _discover_driver_specs()

_driver_tasks: dict[str, asyncio.Task] = {}


def _driver_active(slug: str) -> bool:
    """Read `active` from /proc/<slug>/spec.yaml. Missing proc → True."""
    try:
        spec = P.read_spec(slug)
    except P.ProcessNotFound:
        return True
    val = spec.get("active", True)
    return bool(val) if isinstance(val, bool) else True


def _ensure_driver_proc(slug: str) -> None:
    """Idempotent proc entry for a long-running kernel-owned driver.

    First spawn writes `kind: driver, active: true`. On subsequent spawns
    the existing spec (including any `active:` flipped by paictl) is left
    untouched."""
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
    effect on reload without a kernel restart."""
    specs = _discover_driver_specs()
    known = {slug for slug, _ in specs}
    for slug, factory in specs:
        active = _driver_active(slug)
        running = slug in _driver_tasks and not _driver_tasks[slug].done()
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
                try:
                    P.append_log(slug, f"failed to start: {e!r}")
                    for line in tb.rstrip().splitlines():
                        P.append_log(slug, f"  {line}")
                    P.resolve(slug, "failed")
                except P.ProcessNotFound:
                    pass
                continue
            _driver_tasks[slug] = task
            print(f"[kernel] driver started: {slug}", flush=True)
        elif not active and running:
            _driver_tasks[slug].cancel()
            try:
                await _driver_tasks[slug]
            except (asyncio.CancelledError, Exception):
                pass
            del _driver_tasks[slug]
            print(f"[kernel] driver stopped: {slug}", flush=True)
    for slug in list(_driver_tasks):
        if slug in known or slug == "proc-watcher":
            continue
        _driver_tasks[slug].cancel()
        try:
            await _driver_tasks[slug]
        except (asyncio.CancelledError, Exception):
            pass
        del _driver_tasks[slug]
        print(f"[kernel] driver removed: {slug}", flush=True)


async def _supervise_driver(slug: str, coro) -> None:
    """Run a driver coroutine under a proc entry.

    Resolves `cancelled` on shutdown (no nudge) and `failed` on crash
    (nudges PAI via the standard proc_resolved path).
    """
    _ensure_driver_proc(slug)
    try:
        await coro
    except asyncio.CancelledError:
        try:
            P.resolve(slug, "cancelled")
        except P.ProcessNotFound:
            pass
        raise
    except Exception as e:
        tb = traceback.format_exc()
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
    contacts.sync(M.PEOPLE_DIR, M.MESSAGES_DIR)
    heap = T.rebuild_from_proc()
    watcher = EventWatcher(P.EVENTS_DIR, loop)
    watcher.start()
    await supervisor.resume_from_disk()
    print(f"[kernel] supervise: started — {len(heap)} timers loaded", flush=True)

    proc_watcher_task = asyncio.create_task(
        _supervise_driver("proc-watcher", proc_watcher.run(heap)),
        name="proc-watcher",
    )
    await _reconcile_drivers()

    try:
        while True:
            now = datetime.now()
            await _drain_elapsed_timers(heap, now)
            timeout = T.time_until_next(heap, datetime.now())

            event_task = asyncio.create_task(watcher.next())
            if timeout is None:
                await event_task
                await _handle_event_file(event_task.result(), heap)
            else:
                sleep_task = asyncio.create_task(asyncio.sleep(timeout))
                done, pending = await asyncio.wait(
                    {event_task, sleep_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for t in pending:
                    t.cancel()
                if event_task in done:
                    await _handle_event_file(event_task.result(), heap)
    except asyncio.CancelledError:
        raise
    except _RestartRequested:
        pass  # finally runs the shutdown; entry.py execs after run() returns
    finally:
        for tasks in _active_nudges.values():
            for t in tasks:
                t.cancel()
        await supervisor.shutdown()
        all_tasks = [proc_watcher_task, *_driver_tasks.values()]
        for t in all_tasks:
            t.cancel()
        for t in all_tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        _driver_tasks.clear()
        watcher.stop()
        try:
            remaining = P.list_procs(status_filter="running")
            # Cron services (have schedule:) outlive kernel restarts by
            # design — leave them running so rebuild_from_proc re-arms
            # the timer on next boot.
            survivors = []
            for slug in remaining:
                try:
                    spec = P.read_spec(slug)
                except P.ProcessNotFound:
                    spec = {}
                if "schedule" in spec:
                    survivors.append(slug)
            to_resolve = [s for s in remaining if s not in survivors]
            if to_resolve:
                print(f"[kernel] shutdown: resolving {len(to_resolve)} procs", flush=True)
                for slug in to_resolve:
                    try:
                        P.resolve(slug, "stopped")
                    except Exception as e:
                        print(f"[kernel] failed to resolve {slug}: {e!r}", flush=True)
            if survivors:
                print(f"[kernel] shutdown: preserving {len(survivors)} cron procs across restart", flush=True)
        except Exception as e:
            print(f"[kernel] shutdown sweep failed: {e!r}", flush=True)
        try:
            await shell_tool.shutdown_all()
        except Exception as e:
            print(f"[kernel] shell_tool shutdown failed: {e!r}", flush=True)
        try:
            run_dir = Path(os.environ.get("PAI_ROOT", str(Path.home() / ".pai"))) / "run"
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
        out = subprocess.check_output(
            ["ps", "-eo", "pid=,pgid="],
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
        out = subprocess.check_output(
            ["ps", "-eo", "pid=,ppid="],
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
