"""Watcher hub for the PAI web surface.

Mirrors the TUI's filesystem watchers (`src/sbin/tui/state.py`) but threading-
based (no asyncio loop) so it can fan out to many SSE clients from inside a
stdlib HTTP server. It is almost entirely read + react: the only state it
mutates is the deliberate build-skew auto-heal (emitting `kernel:restart` when
it detects the kernel is running an older build than this console — see
`_recompute_build`). All other writes the web surface performs live in
`actions.py` and mirror the TUI (a me-thread day-file line + an event file).

Pure parsing/format helpers are imported from `sbin.tui.state` so the on-disk
message format has a single source of truth across both surfaces.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

from boot import build
from boot import config
from boot import paths
from boot.nudge import DEFAULT_COMPACT_THRESHOLD
from boot.processes import (
    EVENTS_DIR,
    PROC_DIR,
    list_active_procs,
    list_procs,
    read_busy,
    read_spec,
    read_status,
)
from boot.proctree import order_as_tree

from . import actions
from . import dashboards
from . import driver_health

from sbin.tui.state import (
    KERNEL_LOG,
    ME_ROOT,
    _MSG_HEADER,
    _infer_type,
    _read_ctx_tokens,
    _read_sighting,
    me_thread_dir,
    slug_for_pid,
    today_file,
)


# How many recent kernel.log lines to ship in the initial snapshot. Matches
# the client-side ring-buffer cap (CAP in App.tsx).
_LOG_BACKLOG_LINES = 500

# Safety-net poll interval for the proc + me-thread watchers. macOS FSEvents
# coalesces bursty writes and can drop a directory-level notification outright,
# so a busy multi-tool turn's final me-thread write (the reply) or a proc
# busy-state flip can be missed — leaving the reply out of the chat and the
# status stuck on a stale value until a reconnect re-seeds. The kernel.log tail
# already guards against this with its own poll (see _watch_log); this mirrors
# it for the two directory watchers. Same cadence as the log tail and the TUI.
_SAFETY_POLL_SECS = 0.5


def _read_log_tail(path: Path, n: int) -> list[str]:
    """Last `n` non-empty lines of `path`, or [] if it can't be read.

    Reads a bounded tail from EOF rather than the whole file — kernel.log
    grows without bound.
    """
    if n <= 0:
        return []
    try:
        size = path.stat().st_size
    except OSError:
        return []
    # ~512 bytes/line is generous; read enough to cover n lines.
    want = min(size, n * 512)
    try:
        with path.open("rb") as f:
            f.seek(size - want)
            data = f.read(want)
    except OSError:
        return []
    text = data.decode("utf-8", errors="replace")
    if want < size:
        # Drop the (likely partial) first line.
        text = text.split("\n", 1)[-1] if "\n" in text else ""
    lines = [ln for ln in text.splitlines() if ln]
    return lines[-n:]


# --- message format (matches widgets._style_message split) -----------------


def parse_thread(text: str) -> list[dict]:
    """Split a day-file into structured messages.

    Each message starts with `^[HH:MM] sender:`; following lines (until the
    next header) belong to the same body. Returns dicts the client renders.
    """
    raw_messages: list[str] = []
    current: list[str] = []
    for ln in text.splitlines():
        if _MSG_HEADER.match(ln):
            if current:
                raw_messages.append("\n".join(current).rstrip())
            current = [ln]
        elif current:
            current.append(ln)
    if current:
        raw_messages.append("\n".join(current).rstrip())

    out: list[dict] = []
    for msg in raw_messages:
        if not msg:
            continue
        out.append(_split_message(msg))
    return out


def _voice_installed() -> bool:
    """True when the `voice` driver bundle is installed — lets the console show
    a live on/off switch for the host-mic listener even while it's stopped (a
    stopped driver drops out of the proc list, so running-state alone can't tell
    'installed but off' from 'not installed')."""
    return (paths.PAI_ROOT / "usr" / "lib" / "drivers" / "voice").exists()


def _split_message(msg: str) -> dict:
    first_nl = msg.find("\n")
    head = msg if first_nl < 0 else msg[:first_nl]
    try:
        rb = head.index("] ")
        colon = head.index(":", rb)
    except ValueError:
        return {"ts": "", "sender": "", "body": msg, "raw": True}
    sender = head[rb + 2 : colon].strip()
    ts = head[1:rb]
    after_colon = head[colon + 1 :]
    first_body = after_colon[1:] if after_colon.startswith(" ") else after_colon
    rest = "" if first_nl < 0 else msg[first_nl:]
    body = first_body + rest
    return {"ts": ts, "sender": sender, "body": body, "raw": False}


def read_thread(pid: int) -> list[dict]:
    # The on-disk transcript is keyed by the PAI's unique slug (pids are reused
    # across reboots/subagents — see paths.me_thread_dir). The hub still keys
    # its in-memory map and the SSE protocol by pid, which is unambiguous within
    # a single running fleet, so resolve pid -> slug only at the disk boundary.
    path = today_file(slug_for_pid(pid))
    if not path.exists():
        return []
    return parse_thread(path.read_text(encoding="utf-8", errors="replace"))


# --- proc rows (matches state.ProcWatcher.next) ----------------------------


def _short_when(when: str) -> str:
    if not when:
        return ""
    try:
        from datetime import datetime

        return datetime.fromisoformat(when).strftime("%m-%d %H:%M")
    except ValueError:
        return when


def read_proc_rows() -> list[dict]:
    specs: list[dict] = []
    for slug in list_active_procs():
        try:
            spec = read_spec(slug)
            status = read_status(slug)
        except Exception:
            continue
        specs.append({**spec, "_slug": slug, "_status": status})

    rows: list[dict] = []
    for spec, prefix in order_as_tree(specs):
        slug = spec["_slug"]
        when = str(spec.get("deadline") or spec.get("schedule") or "")
        parent = spec.get("parent")
        pid_val = spec.get("pid")
        busy = read_busy(slug)
        rows.append(
            {
                "slug": slug,
                "pid": str(pid_val) if isinstance(pid_val, int) else "",
                "type": _infer_type(spec),
                "parent": str(parent) if parent is not None else "",
                "when": when,
                "when_short": _short_when(when),
                "description": str(spec.get("description", "")),
                "status": spec["_status"],
                "tree_prefix": prefix,
                "busy": {"reason": busy[0], "started_at": busy[1]} if busy else None,
                "ctx_tokens": _read_ctx_tokens(slug),
                # Compaction trips at this many prompt-window tokens — the
                # natural "full" mark for the composer's context ring. Per-PAI
                # `compact_threshold:` (a config-managed field on spec.yaml)
                # overrides the kernel default.
                "ctx_limit": int(spec.get("compact_threshold") or DEFAULT_COMPACT_THRESHOLD),
            }
        )
    return rows


def read_plan(slug: str) -> str:
    """Raw text of a PAI's live `proc/<slug>/plan.md`, or '' if absent/empty.

    The PAI authors this itself via plain shell (no wrapper tool) for genuinely
    multi-step work — a GFM checklist the console renders as a live plan strip.
    Empty/whitespace-only counts as no plan so an emptied file drops the strip.
    """
    try:
        text = (PROC_DIR / slug / "plan.md").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text if text.strip() else ""


def write_plan(slug: str, content: str) -> None:
    """Owner edit of a PAI's live `proc/<slug>/plan.md` (console checkboxes,
    step add/remove, raw-markdown edits).

    The PAI still owns the file — this is the owner reaching into the same
    surface, so it keeps the same protocol: whitespace-only content removes the
    file (the owner's equivalent of the PAI's `rm` once every goal is done).
    Written atomically so the hub's /proc watcher never broadcasts a half-write.
    Last write wins if the PAI rewrites the plan concurrently.
    """
    path = PROC_DIR / slug / "plan.md"
    if not content.strip():
        path.unlink(missing_ok=True)
        return
    tmp = path.parent / ".plan.md.tmp"
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


def read_fleet() -> list[dict]:
    """Running kind:pai procs, with fallback flagged. Drives the tabs."""
    fleet: list[dict] = []
    for slug in list_procs(status_filter="running"):
        try:
            spec = read_spec(slug)
        except Exception:
            continue
        if spec.get("kind") != "pai":
            continue
        pid = spec.get("pid")
        if not isinstance(pid, int):
            continue
        fleet.append(
            {
                "pid": pid,
                "slug": slug,
                "fallback": spec.get("fallback") is True,
                # clone_of is a behavior-free marker that lives only in
                # /etc/config.yaml (not the proc spec), so read it from there —
                # it's what gates the "−" delete button on the frontend.
                "clone_of": config.clone_of(slug),
                # Owner-chosen display name (config `display_name:`, projected
                # into the spec by reconcile). Falls back to the slug.
                "title": str(spec.get("display_name") or "").strip() or slug,
                # Idle heartbeat interval ("30m"/int seconds), projected into
                # the spec by reconcile; null/absent = off. Drives the console
                # Heartbeat button label + modal current value.
                "heartbeat": spec.get("heartbeat"),
            }
        )
    fleet.sort(key=lambda f: f["pid"])
    return fleet


# --- watchdog plumbing -----------------------------------------------------


class _Poke(FileSystemEventHandler):
    """Calls `cb(path)` on any FS event (optionally filtered)."""

    def __init__(self, cb: Callable[[str], None]):
        self._cb = cb

    def on_any_event(self, event) -> None:  # type: ignore[override]
        self._cb(getattr(event, "dest_path", None) or event.src_path)


class _Debounced:
    """A worker thread that runs `fn` shortly after `poke()`, coalescing bursts."""

    def __init__(self, fn: Callable[[], None], debounce: float = 0.06):
        self._fn = fn
        self._debounce = debounce
        self._ev = threading.Event()
        self._stop = False
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def poke(self) -> None:
        self._ev.set()

    def _loop(self) -> None:
        while not self._stop:
            self._ev.wait()
            if self._stop:
                return
            time.sleep(self._debounce)
            self._ev.clear()
            try:
                self._fn()
            except Exception:  # never let a watcher thread die
                import traceback

                traceback.print_exc()

    def stop(self) -> None:
        self._stop = True
        self._ev.set()


# --- the hub ---------------------------------------------------------------


class Hub:
    """Owns the observers and fans state out to subscribers as dict messages.

    Subscribers receive: hello (once), procs, fleet, thread, event, log, plan.
    """

    def __init__(self):
        self._subs: set["Subscriber"] = set()
        self._lock = threading.Lock()
        self._observers: list[Observer] = []
        self._workers: list[_Debounced] = []
        self._fleet_pids: list[int] = []
        self._threads: dict[int, list[dict]] = {}
        self._procs: list[dict] = []
        self._fleet: list[dict] = []
        self._pending_approvals: list[dict] = []
        self._scheduled: list[dict] = []
        self._send_caps: list[dict] = []
        self._drivers: list[dict] = []
        self._dashboards: list[dict] = []
        self._plans: dict[int, str] = {}
        self._notetaker_recording = False
        self._log_offset = 0
        # Build-skew detection: this console's build is fixed for its lifetime;
        # the kernel's is read from its boot stamp. _heal holds the auto-reboot
        # cooldown/escalation state (see _recompute_build).
        console = build.running_build()
        self._console_build = console.version
        self._console_dev = console.dev
        self._build_status: dict = {}
        self._heal = build.HealState()
        self._build_recheck: Optional[threading.Timer] = None
        # Set by the serving entrypoint (`pai start` / `pai-web`): replaces this
        # process with a fresh image so a *console*-side stale build can heal
        # itself the way the kernel does via `kernel:restart`. os.exec* — never
        # returns on success. None when the embedder can't be re-exec'd.
        self.console_restart: Optional[Callable[[], None]] = None

    # -- subscriptions --
    def add(self, sub: "Subscriber") -> None:
        with self._lock:
            self._subs.add(sub)

    def remove(self, sub: "Subscriber") -> None:
        with self._lock:
            self._subs.discard(sub)

    def _broadcast(self, msg: dict) -> None:
        with self._lock:
            subs = list(self._subs)
        for s in subs:
            s.send(msg)

    def snapshot(self) -> dict:
        """Full initial state for a freshly connected client."""
        with self._lock:
            return {
                "type": "hello",
                "voice_installed": _voice_installed(),
                "fleet": list(self._fleet),
                "procs": list(self._procs),
                "pending_approvals": list(self._pending_approvals),
                "scheduled": list(self._scheduled),
                "send_capabilities": list(self._send_caps),
                "drivers": list(self._drivers),
                "dashboards": list(self._dashboards),
                "plans": {str(pid): md for pid, md in self._plans.items()},
                "notetaker_recording": self._notetaker_recording,
                "build": dict(self._build_status),
                "threads": {str(pid): msgs for pid, msgs in self._threads.items()},
                # The live tail only streams *new* lines (starts at EOF), so a
                # fresh client would see an empty feed despite a populated log.
                # Ship the recent backlog so the log/activity panes have context
                # on connect.
                "log_backlog": _read_log_tail(KERNEL_LOG, _LOG_BACKLOG_LINES),
            }

    # -- lifecycle --
    def start(self) -> None:
        # Compute initial state synchronously.
        self._recompute_procs(broadcast=False)
        for pid in self._fleet_pids:
            self._threads[pid] = read_thread(pid)

        # Tail starts at EOF (only new lines), like the TUI.
        KERNEL_LOG.parent.mkdir(parents=True, exist_ok=True)
        if not KERNEL_LOG.exists():
            KERNEL_LOG.touch()
        self._log_offset = KERNEL_LOG.stat().st_size

        proc_worker = _Debounced(lambda: self._recompute_procs(broadcast=True))
        me_worker = _Debounced(self._recompute_threads)
        # Owner scheduled tasks are paicron procs, so they ride the same /proc
        # watch: a spec write (create) or status flip (delete/edit) pokes this,
        # and the change-gated recompute rebroadcasts the list.
        scheduled_worker = _Debounced(lambda: self._recompute_scheduled(broadcast=True))
        # Driver health rides the same event-driven transport: /proc writes
        # (status flips, health.yaml breadcrumbs) and /sys/drivers writes
        # (cursor updates) poke it; the safety poll below also re-runs the
        # change-gated classification so a staleness or crash-loop window
        # expiring flips the panel without any new mechanism.
        drivers_worker = _Debounced(lambda: self._recompute_drivers(broadcast=True))
        # A PAI's live plan.md sits under proc/<slug>/, already inside the
        # recursively-watched PROC_DIR — so a plan write/tick/rm already pokes
        # this observer; it just needs its own change-gated recompute+broadcast.
        plans_worker = _Debounced(lambda: self._recompute_plans(broadcast=True))
        proc_worker.start()
        me_worker.start()
        drivers_worker.start()
        scheduled_worker.start()
        plans_worker.start()
        self._workers += [proc_worker, me_worker, drivers_worker, scheduled_worker, plans_worker]

        self._watch(
            PROC_DIR,
            lambda _p: (
                proc_worker.poke(),
                drivers_worker.poke(),
                scheduled_worker.poke(),
                plans_worker.poke(),
            ),
            recursive=True,
        )
        self._recompute_scheduled(broadcast=False)
        self._recompute_plans(broadcast=False)

        sys_drivers_dir = paths.PAI_ROOT / "sys" / "drivers"
        sys_drivers_dir.mkdir(parents=True, exist_ok=True)
        self._watch(sys_drivers_dir, lambda _p: drivers_worker.poke(), recursive=True)
        self._recompute_drivers(broadcast=False)

        ME_ROOT.mkdir(parents=True, exist_ok=True)
        self._watch(ME_ROOT.resolve(), lambda _p: me_worker.poke(), recursive=True)

        # Backstop the directory watchers against dropped FSEvents (see
        # _SAFETY_POLL_SECS). Poking is idempotent — the recomputes only
        # broadcast on a real change — so an extra tick costs a couple of stats.
        self._start_safety_poll(
            [proc_worker, me_worker, drivers_worker, scheduled_worker, plans_worker]
        )

        EVENTS_DIR.mkdir(parents=True, exist_ok=True)
        self._watch_events()
        self._watch_log()

        # Owner approval queue: badge + modal feed for draft-and-approve.
        approvals_dir = paths.var_spool_approvals()
        approvals_dir.mkdir(parents=True, exist_ok=True)
        approvals_worker = _Debounced(lambda: self._recompute_approvals(broadcast=True))
        approvals_worker.start()
        self._workers.append(approvals_worker)
        self._watch(approvals_dir, lambda _p: approvals_worker.poke(), recursive=False)
        self._recompute_approvals(broadcast=False)

        # Send-permission control: mode per mounted channel. Both the mode and
        # which channels are mounted derive from /etc/config.yaml, so a single
        # watch on etc/ keeps the sidebar in step with the file — whether the
        # edit came from the toggle itself or a hand-edit.
        etc_dir = paths.etc()
        etc_dir.mkdir(parents=True, exist_ok=True)
        caps_worker = _Debounced(lambda: self._recompute_send_caps(broadcast=True))
        caps_worker.start()
        self._workers.append(caps_worker)
        self._watch(etc_dir, lambda _p: caps_worker.poke(), recursive=False)
        self._recompute_send_caps(broadcast=False)

        # Notetaker recording indicator: the driver holds a `recording` marker
        # file while a call is being captured; the console must show it (PAI
        # never records silently).
        notetaker_dir = paths.PAI_ROOT / "sys" / "drivers" / "notetaker"
        notetaker_dir.mkdir(parents=True, exist_ok=True)
        rec_worker = _Debounced(lambda: self._recompute_notetaker(broadcast=True))
        rec_worker.start()
        self._workers.append(rec_worker)
        self._watch(notetaker_dir, lambda _p: rec_worker.poke(), recursive=False)
        self._recompute_notetaker(broadcast=False)

        # Build-skew: recompute when the kernel restamps its build (reboot) or
        # when `pai update` moves the installed release (`.release` marker).
        build_worker = _Debounced(lambda: self._recompute_build(broadcast=True))
        build_worker.start()
        self._workers.append(build_worker)
        stamp_dir = build.kernel_stamp_path().parent
        stamp_dir.mkdir(parents=True, exist_ok=True)
        self._watch(stamp_dir, lambda _p: build_worker.poke(), recursive=False)
        var_lib = paths.PAI_ROOT / "var" / "lib"
        var_lib.mkdir(parents=True, exist_ok=True)
        self._watch(var_lib, lambda _p: build_worker.poke(), recursive=False)
        self._recompute_build(broadcast=False)

        # PAI-authored dashboards: a `<slug>.html` write/delete under
        # /var/lib/dashboards/ pokes this, and the change-gated recompute
        # rebroadcasts the tab list — a new dashboard appears (and a deleted one
        # disappears) live, same FS-observer → broadcast path as scheduled tasks.
        dashboards_worker = _Debounced(lambda: self._recompute_dashboards(broadcast=True))
        dashboards_worker.start()
        self._workers.append(dashboards_worker)
        dashboards_dir = paths.var_lib_dashboards()
        dashboards_dir.mkdir(parents=True, exist_ok=True)
        self._watch(dashboards_dir, lambda _p: dashboards_worker.poke(), recursive=False)
        self._recompute_dashboards(broadcast=False)

    def _watch(self, path: Path, cb, recursive: bool) -> None:
        obs = Observer()
        obs.schedule(_Poke(cb), str(path), recursive=recursive)
        obs.start()
        self._observers.append(obs)

    def _safety_tick(self, workers: list["_Debounced"]) -> None:
        """One safety-net poll tick: poke every FS-driven worker so a coalesced
        or dropped FSEvents notification still gets reconciled on the next tick.
        Extracted from the poll loop so it can be exercised deterministically."""
        for w in workers:
            w.poke()

    def _start_safety_poll(self, workers: list["_Debounced"]) -> None:
        def poll() -> None:
            while True:
                time.sleep(_SAFETY_POLL_SECS)
                self._safety_tick(workers)

        threading.Thread(target=poll, daemon=True, name="hub-safety-poll").start()

    def stop(self) -> None:
        if self._build_recheck is not None:
            self._build_recheck.cancel()
        for w in self._workers:
            w.stop()
        for obs in self._observers:
            obs.stop()
        for obs in self._observers:
            obs.join(timeout=2)

    # -- recompute handlers --
    def _recompute_procs(self, broadcast: bool) -> None:
        rows = read_proc_rows()
        fleet = read_fleet()
        new_pids = [f["pid"] for f in fleet]
        # Gate the broadcast on an actual change: the safety-net poll drives
        # this every _SAFETY_POLL_SECS, so an unconditional emit would spam
        # every client with identical rows twice a second. (The frontend
        # derives the live elapsed counter itself, so unchanged rows need no
        # re-send.) _recompute_threads is already change-gated the same way.
        rows_changed = rows != self._procs
        self._procs = rows
        self._fleet = fleet

        # Reconcile per-PAI thread snapshots against the running fleet.
        added = [p for p in new_pids if p not in self._fleet_pids]
        removed = [p for p in self._fleet_pids if p not in new_pids]
        self._fleet_pids = new_pids
        for pid in removed:
            self._threads.pop(pid, None)
        for pid in added:
            self._threads[pid] = read_thread(pid)

        if broadcast:
            if rows_changed:
                self._broadcast({"type": "procs", "rows": rows})
            if added or removed:
                self._broadcast({"type": "fleet", "fleet": fleet})
                for pid in added:
                    self._broadcast(
                        {"type": "thread", "pid": pid, "messages": self._threads[pid]}
                    )

    def _recompute_approvals(self, broadcast: bool) -> None:
        pend = actions.list_pending()
        if pend == self._pending_approvals:
            return
        self._pending_approvals = pend
        if broadcast:
            self._broadcast({"type": "pending_approvals", "approvals": pend})

    def _recompute_scheduled(self, broadcast: bool) -> None:
        try:
            tasks = actions.list_scheduled()
        except Exception:  # never let a watcher thread die
            import traceback

            traceback.print_exc()
            return
        if tasks == self._scheduled:
            return
        self._scheduled = tasks
        if broadcast:
            self._broadcast({"type": "scheduled", "tasks": tasks})

    def _recompute_send_caps(self, broadcast: bool) -> None:
        caps = actions.list_send_capabilities()
        if caps == self._send_caps:
            return
        self._send_caps = caps
        if broadcast:
            self._broadcast({"type": "send_capabilities", "capabilities": caps})

    def _recompute_drivers(self, broadcast: bool) -> None:
        try:
            rows = driver_health.read_rows()
        except Exception:  # never let a watcher thread die
            import traceback

            traceback.print_exc()
            return
        if rows == self._drivers:
            return
        self._drivers = rows
        if broadcast:
            self._broadcast({"type": "drivers", "drivers": rows})

    def _recompute_dashboards(self, broadcast: bool) -> None:
        try:
            rows = dashboards.list_dashboards()
        except Exception:  # never let a watcher thread die
            import traceback

            traceback.print_exc()
            return
        if rows == self._dashboards:
            return
        self._dashboards = rows
        if broadcast:
            self._broadcast({"type": "dashboards", "dashboards": rows})

    def _recompute_plans(self, broadcast: bool) -> None:
        """Per-PAI live plan.md, keyed by pid. Change-gated like the other
        recomputes so the safety poll doesn't spam identical strips. Reads off
        the current fleet (populated by _recompute_procs) so a plan only shows
        for a running PAI; a subagent's plan.md is reaped with its proc dir."""
        plans: dict[int, str] = {}
        for f in self._fleet:
            md = read_plan(f["slug"])
            if md:
                plans[f["pid"]] = md
        if plans == self._plans:
            return
        self._plans = plans
        if broadcast:
            self._broadcast(
                {"type": "plan", "plans": {str(pid): md for pid, md in plans.items()}}
            )

    def _recompute_notetaker(self, broadcast: bool) -> None:
        recording = (
            paths.PAI_ROOT / "sys" / "drivers" / "notetaker" / "recording"
        ).exists()
        if recording == self._notetaker_recording:
            return
        self._notetaker_recording = recording
        if broadcast:
            self._broadcast({"type": "notetaker_recording", "recording": recording})

    def _recompute_build(self, broadcast: bool) -> None:
        """Classify kernel-vs-console build skew and auto-heal a stale kernel.

        Ground truth: the console's build (fixed at start) vs the kernel's boot
        stamp vs the installed `current` release. When the kernel is behind and
        this console is current, emit `kernel:restart` once (then a re-check
        timer catches a reboot that didn't take and escalates to a banner). When
        the console itself is behind, only warn — rebooting can't fix that."""
        stamp = build.read_kernel_stamp()
        kernel_ver = stamp.get("version") if stamp else None
        current = build.current_release()
        console = self._console_build
        state = build.classify_skew(kernel_ver, console, current)

        action = build.decide_heal(kernel_ver, console, current, self._heal, time.monotonic())
        if action == "reboot":
            self._heal.last_kernel_ver = kernel_ver
            self._heal.last_attempt_monotonic = time.monotonic()
            self._heal.escalated = False
            try:
                actions.reboot_kernel()
            except Exception:  # never let auto-heal kill the watcher thread
                pass
            self._schedule_build_recheck()
        elif action == "escalate":
            self._heal.escalated = True
        elif action == "warn_console":
            # This console is the stale side: `pai update` swapped the release
            # under us, and the kernel (already current) can't fix that. Re-exec
            # ourselves into the new build; on refusal/failure the state below
            # still ships the console_stale banner.
            self._maybe_restart_console(current)
        if state == "in_sync":
            self._heal = build.HealState()

        status = {
            "state": state,
            "kernel": kernel_ver,
            "console": console,
            "current": current,
            "escalated": self._heal.escalated,
        }
        if status != self._build_status:
            self._build_status = status
            if broadcast:
                self._broadcast({"type": "build", "status": status})

    def _maybe_restart_console(self, current: str) -> None:
        """Heal a stale console by replacing this process with a fresh image.

        Event-driven: reached only from `_recompute_build`, which the kernel's
        restamp (post-`kernel:restart`) or a `.release` marker write pokes via
        FS watch. One attempt per release rides `CONSOLE_REEXEC_ENV` — the
        environment survives the exec, so a restart that came back still stale
        falls through to the banner instead of exec-looping."""
        if not build.decide_console_restart(
            self._console_build,
            current,
            dev=self._console_dev,
            already=os.environ.get(build.CONSOLE_REEXEC_ENV),
            can_restart=self.console_restart is not None,
        ):
            return
        os.environ[build.CONSOLE_REEXEC_ENV] = current
        print(
            f"[web] console build {self._console_build} is behind installed "
            f"{current} — re-exec'ing the web surface",
            file=sys.stderr,
            flush=True,
        )
        try:
            self.console_restart()  # os.exec* — no return on success
        except Exception:  # never let a failed exec kill the watcher thread
            import traceback

            traceback.print_exc()

    def _schedule_build_recheck(self) -> None:
        """After a reboot attempt, re-evaluate once the cooldown has elapsed —
        a successful reboot restamps kernel.json (FS event handles it), but a
        reboot that silently didn't take produces no event, so this timer is the
        only path to the escalation banner."""
        if self._build_recheck is not None:
            self._build_recheck.cancel()
        t = threading.Timer(65.0, lambda: self._recompute_build(broadcast=True))
        t.daemon = True
        self._build_recheck = t
        t.start()

    def _recompute_threads(self) -> None:
        for pid in list(self._fleet_pids):
            msgs = read_thread(pid)
            if msgs != self._threads.get(pid):
                self._threads[pid] = msgs
                self._broadcast({"type": "thread", "pid": pid, "messages": msgs})

    # -- events --
    def _watch_events(self) -> None:
        def on_path(raw: str) -> None:
            p = Path(raw)
            if p.suffix != ".yaml" or p.name.startswith("."):
                return
            sight = _read_sighting(p)  # read now, beat the kernel's unlink
            payload = sight.payload
            if payload.get("_gone"):
                suffix = sight.filename.rsplit("-", 1)[-1]
                source = suffix.removesuffix(".yaml") or "?"
                kind = "(consumed)"
                consumed = True
            else:
                source = str(payload.get("source", "?"))
                kind = str(payload.get("kind", "?"))
                consumed = False
            pai_ref = (
                payload.get("slug")
                or payload.get("target_pid")
                or payload.get("pai")
                or ""
            )
            target = (
                payload.get("thread")
                or payload.get("handle")
                or payload.get("slug")
                or ""
            )
            self._broadcast(
                {
                    "type": "event",
                    "at": sight.at.strftime("%H:%M:%S"),
                    "source": source,
                    "kind": kind,
                    "target": str(target),
                    "pai": str(pai_ref) if pai_ref else "",
                    "consumed": consumed,
                }
            )

            # Voice activity also rides a dedicated, lightweight `voice` message
            # so the composer can show "Speaking: …" (wake fired) and the final
            # phrase heard — without the client having to special-case the noisy
            # generic event feed. Only the live sighting carries kind/text; the
            # `_gone` (consumed) echo doesn't, so skip it.
            if not consumed and source == "voice" and kind in ("listening", "utterance"):
                self._broadcast(
                    {
                        "type": "voice",
                        "phase": kind,
                        "text": str(payload.get("text") or "") if kind == "utterance" else "",
                    }
                )

        # Only created/moved files are new events; on_any would double-fire.
        class _EvHandler(FileSystemEventHandler):
            def on_created(self, event):
                if not event.is_directory:
                    on_path(event.src_path)

            def on_moved(self, event):
                if not event.is_directory and getattr(event, "dest_path", None):
                    on_path(event.dest_path)

        obs = Observer()
        obs.schedule(_EvHandler(), str(EVENTS_DIR), recursive=False)
        obs.start()
        self._observers.append(obs)

    # -- kernel.log tail --
    def _watch_log(self) -> None:
        def read_new() -> None:
            try:
                size = KERNEL_LOG.stat().st_size
            except FileNotFoundError:
                return
            if size < self._log_offset:
                self._log_offset = 0  # truncated/rotated
            if size == self._log_offset:
                return
            try:
                with KERNEL_LOG.open("rb") as f:
                    f.seek(self._log_offset)
                    chunk = f.read(size - self._log_offset)
                self._log_offset = size
            except FileNotFoundError:
                return
            for line in chunk.decode("utf-8", errors="replace").splitlines():
                if line:
                    self._broadcast({"type": "log", "line": line})

        worker = _Debounced(read_new, debounce=0.02)
        worker.start()
        self._workers.append(worker)
        self._watch(KERNEL_LOG.parent, lambda _p: worker.poke(), recursive=False)

        # Safety-net poll (FSEvents coalesces bursts), same as the TUI.
        def poll():
            while True:
                time.sleep(0.5)
                worker.poke()

        threading.Thread(target=poll, daemon=True).start()


class Subscriber:
    """A bounded mailbox for one SSE connection."""

    def __init__(self, maxsize: int = 1000):
        import queue

        self._q: "queue.Queue[Optional[dict]]" = queue.Queue(maxsize=maxsize)

    def send(self, msg: dict) -> None:
        try:
            self._q.put_nowait(msg)
        except Exception:
            pass  # slow client: drop rather than block the broadcaster

    def get(self, timeout: float):
        import queue

        try:
            return self._q.get(timeout=timeout)
        except queue.Empty:
            return None

    def close(self) -> None:
        self._q.put_nowait(None)
