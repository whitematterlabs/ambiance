"""Boot-time event-storm collapse.

When the kernel has been down long enough for upstream drivers to back up
(overnight, days), many events accumulate in /run/pai/events/. Without
intervention, EventWatcher.start()'s catch-up pass would dispatch each one
as its own nudge — N sequential LLM turns for N files.

This phase runs *before* the watcher starts. It scans the spool, groups
events by primary target pid, and for any PAI over THRESHOLD events archives
the originals into /var/log/events/backfill/<boot-ts>/pid-<pid>/ and emits
a single synthetic `kernel:backfill` event with counts + manifest_glob. The
PAI wakes once, sees what it missed, and drills in only where it cares.

Boot-only — runtime batching has different ordering/bypass constraints that
do not apply here (no in-flight turn, no owner mid-conversation).

PAI-side: nothing to configure. The synthetic event carries `target_pid`
and the kernel's `kernel:backfill` branch in boot.main dispatches by pid
directly, sidestepping wake_on globbing entirely.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from pathlib import Path

import yaml

from .. import paths
from .. import processes as P
from ..routing import route_to_pids

# Per-PAI event count above which we collapse into a single kernel:backfill.
# At or below this we let the watcher catch-up dispatch them individually —
# the LLM-turn cost is bounded and the PAI gets per-event fidelity.
THRESHOLD = 10


def _public_kind(event: dict) -> str:
    """Best-effort mapping from a raw event to the kind route_to_pids
    matches against wake_on globs. Mirrors the generic driver routing branch
    in boot.main; iMessage/email special-cases (new_message → imessage:new
    etc.) are not modeled here. Those events fall through to the fallback
    PAI, which is where they were going to land anyway — close enough for
    storm detection."""
    kind = event.get("kind") or ""
    source = event.get("source")
    if isinstance(source, str) and source and source != "kernel" and kind:
        return f"{source}:{kind}"
    return kind


def _load(path: Path) -> dict | None:
    try:
        with path.open() as f:
            data = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        return None
    return data if isinstance(data, dict) else None


def run() -> None:
    spool = paths.EVENTS_DIR
    if not spool.is_dir():
        return
    files = sorted(
        p for p in spool.iterdir()
        if p.is_file() and p.suffix == ".yaml" and not p.name.startswith(".")
    )
    if len(files) <= THRESHOLD:
        # Cheap exit — even if these are all for one PAI, they're under
        # threshold and individual dispatch is fine.
        return

    # Group by primary pid (the first hit from route_to_pids). Each file
    # belongs to exactly one bucket — fan-out routing is collapsed to the
    # primary here so storms split cleanly.
    by_pid: dict[int, list[tuple[Path, str]]] = defaultdict(list)
    for path in files:
        event = _load(path)
        if event is None:
            continue
        pids = route_to_pids(_public_kind(event))
        if not pids:
            continue
        primary = pids[0]
        raw_kind = event.get("kind") or "unknown"
        by_pid[primary].append((path, raw_kind))

    storms = {pid: items for pid, items in by_pid.items() if len(items) > THRESHOLD}
    if not storms:
        return

    boot_ts = datetime.now().strftime("%Y%m%dT%H%M%S")
    archive_root = paths.var_log() / "events" / "backfill" / boot_ts

    for pid, items in storms.items():
        by_kind: dict[str, int] = defaultdict(int)
        for _, k in items:
            by_kind[k] += 1

        mtimes: list[float] = []
        for src, _ in items:
            try:
                mtimes.append(src.stat().st_mtime)
            except OSError:
                pass
        window: dict[str, str] = {}
        if mtimes:
            window = {
                "from": datetime.fromtimestamp(min(mtimes)).isoformat(timespec="seconds"),
                "to": datetime.fromtimestamp(max(mtimes)).isoformat(timespec="seconds"),
            }

        pid_dir = archive_root / f"pid-{pid}"
        pid_dir.mkdir(parents=True, exist_ok=True)

        # Step 1: emit the synthetic event first (atomic tmp+rename via
        # emit_event). If we crash between this and the archive move, the
        # leftover originals will either join a future backfill or dispatch
        # normally — the backfill nudge still describes what should have
        # been there, and nothing is silently dropped.
        payload = {
            "source": "kernel",
            "kind": "kernel:backfill",
            "target_pid": int(pid),
            "count": len(items),
            "by_kind": dict(sorted(by_kind.items())),
            "manifest_glob": str(pid_dir / "*.yaml"),
            "window": window,
        }
        P.emit_event(payload)

        # Step 2: move originals into the archive.
        for src, _ in items:
            try:
                src.rename(pid_dir / src.name)
            except FileNotFoundError:
                pass
            except OSError as e:
                print(f"[boot] backfill: archive {src.name} failed: {e}", flush=True)

        kind_summary = ", ".join(f"{k}={v}" for k, v in sorted(by_kind.items()))
        print(
            f"[boot] backfill: collapsed {len(items)} events for pid={pid} "
            f"({kind_summary})",
            flush=True,
        )
