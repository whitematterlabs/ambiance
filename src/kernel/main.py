"""The kernel loop — tickless, event + timer driven.

Sleeps on whichever fires first: an FS event in live/events/ or the next
pending timer. When the heap is empty and no events are pending, blocks
indefinitely on the watcher.
"""

from __future__ import annotations

import asyncio
import sys
import traceback
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

import yaml

from drivers.imessage import inbound as imessage_in
from drivers.imessage import outbound as imessage_out

from . import contacts
from . import messages as M
from . import processes as P
from . import proc_watcher
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

    if kind == "new_message":
        # TUI-originated owner messages: the line is already appended to
        # me/YYYY-MM-DD.md by the client, so we skip ingest() and just nudge.
        if event.get("thread") == "me":
            text = event.get("text") or ""
            if not text:
                print(f"[kernel] dropping empty owner message: {event!r}", flush=True)
                return
            day = date.today().isoformat()
            _dispatch_nudge(
                1,
                "owner message",
                context={
                    "thread": "me",
                    "sender": "me",
                    "text": text,
                    "day_file": f"communication/messages/me/1/{day}.md",
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
        result = M.ingest(
            handle=handle,
            text=text,
            chat_guid=event.get("chat_guid"),
            display_name=event.get("display_name"),
            received_at=received_at,
            source=event.get("source"),
        )
        tag = "new message"
        if result.created_thread:
            tag += " (new thread)"
        _dispatch_nudge(
            1,
            tag,
            context={
                "thread": result.slug,
                "sender": result.sender,
                "text": text,
                "day_file": str(result.day_file.relative_to(P.LIVE_DIR)),
            },
        )

    elif kind == "messages_backlog":
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
        _dispatch_nudge(
            1,
            "messages backlog",
            context={
                "since": since,
                "threads": summary,
                "total": sum(b["inbound"] + b["outbound"] for b in per_thread.values()),
            },
        )

    elif kind == "proc_resolved":
        slug = event.get("slug")
        status = event.get("status")
        if not slug:
            print(f"[kernel] dropping malformed proc_resolved event: {event!r}", flush=True)
            return
        pai = int(event.get("parent", 1))
        _dispatch_nudge(pai, f"proc {status}", slug=slug, context={"status": status})

    elif kind == "pai_kickoff":
        target_pid = event.get("target_pid")
        text = event.get("text") or ""
        sender_pid = event.get("sender_pid")
        if target_pid is None:
            print(f"[kernel] dropping malformed pai_kickoff event: {event!r}", flush=True)
            return
        _dispatch_nudge(
            int(target_pid),
            "subagent kickoff",
            from_=int(sender_pid) if sender_pid is not None else None,
            context={"text": text},
        )

    elif kind == "send_failed":
        thread = event.get("thread")
        text = event.get("text") or ""
        reason = event.get("reason") or ""
        if not thread:
            print(f"[kernel] dropping malformed send_failed event: {event!r}", flush=True)
            return
        _dispatch_nudge(
            1,
            "send failed",
            context={"thread": thread, "text": text, "reason": reason},
        )

    elif kind == "cron_fired":
        slug = event.get("slug")
        rc = event.get("rc")
        if not slug:
            print(f"[kernel] dropping malformed cron_fired event: {event!r}", flush=True)
            return
        pai = int(event.get("parent", 1))
        _dispatch_nudge(pai, f"cron fired (rc={rc})", slug=slug, context={"rc": rc})

    else:
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
    log_path = P.LIVE_DIR / "tmp" / "kernel.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    f = log_path.open("a", buffering=1, encoding="utf-8")
    sys.stdout = _Tee(sys.stdout, f)
    sys.stderr = _Tee(sys.stderr, f)


def _ensure_pai_1() -> None:
    """Migrate flat me/*.md into me/1/*.md and ensure pid 1 exists."""
    me_dir = P.LIVE_DIR / "communication" / "messages" / "me"
    pid1_dir = me_dir / "1"
    if me_dir.exists():
        pid1_dir.mkdir(parents=True, exist_ok=True)
        for child in me_dir.iterdir():
            if not child.is_file() or child.suffix != ".md":
                continue
            dest = pid1_dir / child.name
            if dest.exists():
                continue
            child.rename(dest)

    have_pai = False
    if P.PROC_DIR.exists():
        for child in P.PROC_DIR.iterdir():
            if not child.is_dir():
                continue
            spec_path = child / "spec.yaml"
            if not spec_path.exists():
                continue
            try:
                with spec_path.open() as f:
                    spec = yaml.safe_load(f) or {}
            except Exception:
                continue
            if spec.get("kind") != "pai":
                continue
            have_pai = True
            # Backfill pid on legacy specs whose slug is the implicit PID.
            if "pid" not in spec and child.name.isdigit():
                spec["pid"] = int(child.name)
                with spec_path.open("w") as f:
                    yaml.safe_dump(spec, f, sort_keys=False)
                print(f"[kernel] backfilled pid={spec['pid']} on {child.name}/spec.yaml", flush=True)
    if not have_pai:
        pid = P.alloc_pai_pid()
        P.spawn_pai(pid)
        print(f"[kernel] spawned pai pid={pid}", flush=True)


def _ensure_driver_proc(slug: str) -> None:
    """Idempotent proc entry for a long-running kernel-owned driver."""
    proc = P.PROC_DIR / slug
    if proc.exists():
        (proc / "status").write_text("running\n")
        try:
            P.append_log(slug, "kernel: restarted")
        except P.ProcessNotFound:
            pass
    else:
        P.spawn(slug, {"kind": "driver"})


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
    _ensure_pai_1()
    contacts.sync_to_people(M.PEOPLE_DIR)
    heap = T.rebuild_from_proc()
    watcher = EventWatcher(P.EVENTS_DIR, loop)
    watcher.start()
    await supervisor.resume_from_disk()
    print(f"[kernel] started — {len(heap)} timers loaded", flush=True)

    drivers = [
        asyncio.create_task(
            _supervise_driver("proc-watcher", proc_watcher.run(heap)),
            name="proc-watcher",
        ),
        asyncio.create_task(
            _supervise_driver("imessage-out", imessage_out.run()),
            name="imessage-out",
        ),
        asyncio.create_task(
            _supervise_driver("imessage-in", imessage_in.run()),
            name="imessage-in",
        ),
    ]

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
    finally:
        for tasks in _active_nudges.values():
            for t in tasks:
                t.cancel()
        await supervisor.shutdown()
        for t in drivers:
            t.cancel()
        for t in drivers:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        watcher.stop()
        print("[kernel] stopped", flush=True)
