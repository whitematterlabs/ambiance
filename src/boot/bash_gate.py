"""Owner approval gate for shell commands (capabilities.bash_exec).

`yes` (the default) — commands run untouched. `no` — every command is
refused. `ask` — a command matching the owner's `bash_allowlist:` prefix
rules (etc/config.yaml) runs directly; anything else is staged as a
`channel: bash` record in var/spool/approvals/ — the same queue the send
gates use, so the console modal pops it — and the tool call BLOCKS until
the owner decides.

Unlike sends, which drivers stage fire-and-forget, a shell command's
tool_result is inline-awaited by the model. So the kernel itself stages the
record, sleeps on its status flip (watchdog on the spool dir — event-driven,
no polling), and delivers the decision as the tool outcome. Fail-closed: an
undecided record expires after _DECISION_TIMEOUT_S and the command is
refused. The approvals driver skips bash records — "delivery" here is
execution, which only the kernel can do.

Cooperative gate, same trust model as the send freezes: it stops an eager
PAI from acting unreviewed, not an adversarial one (the tool runs as the
same unix user).
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import yaml

from . import cmd_allowlist, config, paths


_DECISION_TIMEOUT_S = 600

_NON_ALNUM = re.compile(r"[^a-z0-9]+")

# One waiter per pending record stem. The spool watchdog wakes every waiter
# on any change in the dir (rare + cheap); each re-reads its own record, so
# spurious wakeups are harmless and there is no per-file race to get right.
_waiters: dict[str, asyncio.Event] = {}
_observer = None
_observer_loop: Optional[asyncio.AbstractEventLoop] = None


@dataclass
class GateDecision:
    allowed: bool
    command: str
    note: Optional[str] = None


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _atomic_dump(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w") as f:
        yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True)
    os.replace(tmp, path)


def _load(path: Path) -> Optional[dict]:
    try:
        with path.open() as f:
            data = yaml.safe_load(f)
    except (OSError, yaml.YAMLError):
        return None
    return data if isinstance(data, dict) else None


def _stage(command: str, pai_slug: str, tool: str) -> Path:
    label = (command.strip().split() or ["bash"])[0]
    slug = _NON_ALNUM.sub("-", label.lower()).strip("-")[:60] or "bash"
    ident = time.strftime("%Y%m%d-%H%M%S") + "-" + slug
    queue = paths.var_spool_approvals()
    queue.mkdir(parents=True, exist_ok=True)
    path = queue / f"{ident}.yaml"
    n = 1
    while path.exists():
        path = queue / f"{ident}-{n}.yaml"
        n += 1
    _atomic_dump(path, {
        "id": path.stem,
        "channel": "bash",
        "status": "pending",
        "created_by": pai_slug or "unknown",
        "created_at": _now(),
        "action": {"command": command, "tool": tool},
        "decided_at": None,
        "decided_by": None,
        "error": None,
    })
    return path


def _wake_all() -> None:
    for ev in _waiters.values():
        ev.set()


def _ensure_observer(loop: asyncio.AbstractEventLoop) -> None:
    global _observer, _observer_loop
    if _observer is not None and _observer_loop is loop:
        return
    if _observer is not None:
        # Loop changed (kernel re-init, test harness): the old observer's
        # call_soon_threadsafe targets a dead loop — replace it.
        try:
            _observer.stop()
        except Exception:
            pass
        _observer = None
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer

    class _Handler(FileSystemEventHandler):
        def on_any_event(self, event) -> None:  # type: ignore[override]
            try:
                loop.call_soon_threadsafe(_wake_all)
            except RuntimeError:
                pass  # loop already closed (shutdown/re-init) — nothing to wake

    # Watch the PARENT (var/spool, recursive), not the queue dir itself: the
    # approvals driver runs in this same process and already watches
    # var/spool/approvals, and macOS FSEvents allows one watch per exact path
    # per process — a second one kills the emitter thread ("Cannot add watch
    # … already scheduled") and the gate goes blind. A different path key
    # sidesteps the collision; child events still propagate.
    queue = paths.var_spool_approvals()
    queue.mkdir(parents=True, exist_ok=True)
    try:
        obs = Observer()
        obs.daemon = True  # never block a kernel exit/re-exec
        obs.schedule(_Handler(), str(queue.parent), recursive=True)
        obs.start()
    except Exception as e:  # noqa: BLE001 — degrade to the re-read backstop
        print(f"[gate] spool watch unavailable ({e!r}); relying on re-reads", flush=True)
        _observer = None
        _observer_loop = loop
        return
    _observer = obs
    _observer_loop = loop


async def _await_decision(path: Path) -> dict:
    loop = asyncio.get_running_loop()
    _ensure_observer(loop)
    stem = path.stem
    ev = asyncio.Event()
    _waiters[stem] = ev
    deadline = loop.time() + _DECISION_TIMEOUT_S
    try:
        while True:
            rec = _load(path)
            if rec is None:
                return {"status": "failed", "error": "approval record vanished"}
            if rec.get("status") != "pending":
                return rec
            remaining = deadline - loop.time()
            if remaining <= 0:
                rec["status"] = "expired"
                rec["error"] = (
                    f"no owner decision within {_DECISION_TIMEOUT_S // 60} minutes"
                )
                rec["decided_at"] = _now()
                _atomic_dump(path, rec)
                return rec
            ev.clear()
            # The watch gives instant wakes; the 2s slice is a backstop — an
            # FSEvents emitter can die silently (thread exception) and there
            # is no cheap way to detect it. Re-reading one YAML every 2s,
            # bounded to the window where a human decision is pending, keeps
            # the gate correct under every watcher pathology.
            try:
                await asyncio.wait_for(ev.wait(), timeout=min(remaining, 2.0))
            except asyncio.TimeoutError:
                pass
    except asyncio.CancelledError:
        # Owner interrupt / turn teardown while pending: expire the record so
        # it leaves the approval tray instead of ghosting there.
        rec = _load(path)
        if rec and rec.get("status") == "pending":
            rec["status"] = "expired"
            rec["error"] = "turn interrupted before a decision"
            rec["decided_at"] = _now()
            _atomic_dump(path, rec)
        raise
    finally:
        _waiters.pop(stem, None)


async def clear(
    command: str,
    *,
    pai_slug: str,
    tool: str = "bash",
    notify: Optional[Callable[[str], None]] = None,
) -> GateDecision:
    """Gate one command. Returns immediately in yes/no mode and for
    allowlisted commands; in ask mode otherwise, blocks on the owner's
    decision. `notify` (the turn's status setter) is called when blocking
    starts so the console shows what the PAI is waiting on."""
    mode = config.capability_modes().get("bash_exec", "no")
    if mode == "yes":
        return GateDecision(True, command)
    if mode == "no":
        return GateDecision(
            False, command,
            "shell commands are disabled (capabilities.bash_exec=no); "
            "tell the owner if their request needs one",
        )
    if cmd_allowlist.command_allowed(command, config.bash_allowlist()):
        return GateDecision(True, command)

    path = _stage(command, pai_slug, tool)
    print(f"[gate] bash approval pending: {path.stem}", flush=True)
    if notify is not None:
        try:
            notify(f"awaiting owner approval: {command}"[:120])
        except Exception:
            pass
    rec = await _await_decision(path)
    status = str(rec.get("status") or "")
    if status == "approved":
        final = str((rec.get("action") or {}).get("command") or command)
        note = None
        if final.strip() != command.strip():
            note = f"owner approved an edited command; what ran: {final}"
        return GateDecision(True, final, note)
    reason = str(rec.get("error") or "")
    if status == "rejected":
        return GateDecision(
            False, command,
            "command rejected by owner" + (f": {reason}" if reason else ""),
        )
    return GateDecision(
        False, command,
        f"command not approved ({status or 'unknown'}"
        + (f": {reason}" if reason else "") + ")",
    )


def sweep_stale() -> int:
    """Expire `pending` bash records with no live waiter — orphans from a
    previous kernel life whose awaiting coroutine died with it. Run at boot;
    safe any time because records currently being awaited are skipped."""
    queue = paths.var_spool_approvals()
    if not queue.exists():
        return 0
    n = 0
    for f in queue.glob("*.yaml"):
        if f.stem in _waiters:
            continue
        rec = _load(f)
        if (
            not rec
            or rec.get("channel") != "bash"
            or rec.get("status") != "pending"
        ):
            continue
        rec["status"] = "expired"
        rec["error"] = "kernel restarted before a decision"
        rec["decided_at"] = _now()
        _atomic_dump(f, rec)
        n += 1
    return n
