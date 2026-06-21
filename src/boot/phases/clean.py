"""Phase 2: clean — wipe ephemeral state from prior boots.

`tmp/` is system-wide ephemeral. `run/pai/events/` may hold stale event
files dropped by drivers between the kernel's last shutdown and this
boot. We do NOT wipe `proc/` here — process state is owned by the
proc-layer migration. Driver coroutines, however, cannot survive across
kernel boots, so a `kind: driver` proc left at `running` is stale until
the supervise loop starts it again.
"""
from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

import yaml

# Import the module, not the name: PAI_ROOT is resolved at import time
# from os.environ. Tests reload boot.paths after monkeypatching PAI_ROOT;
# a `from ..paths import PAI_ROOT` would capture the pre-reload value.
from .. import paths


def _wipe_dir_contents(path: Path) -> None:
    if not path.is_dir():
        return
    for child in path.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()


def _wipe_busy_flags() -> None:
    """Drop any stale `busy` flags left by a prior crashed kernel. Each
    nudge writes /proc/<slug>/busy and clears it in a finally; if the
    kernel died mid-nudge, the flag is a phantom."""
    if not paths.PROC_DIR.is_dir():
        return
    for child in paths.PROC_DIR.iterdir():
        if not child.is_dir():
            continue
        (child / "busy").unlink(missing_ok=True)


def _reset_stale_driver_statuses() -> None:
    """Clear stale driver `running` statuses before boot hooks run.

    Drivers are in-kernel coroutines. At this point in boot none of them has
    been started yet, so `running` can only be leftover disk state from a
    prior unclean shutdown. Active drivers will be marked running again by
    `_reconcile_drivers()` after hooks and event-spool backfill complete.
    """
    if not paths.PROC_DIR.is_dir():
        return
    for child in paths.PROC_DIR.iterdir():
        if not child.is_dir():
            continue
        spec_path = child / "spec.yaml"
        status_path = child / "status"
        if not spec_path.is_file() or not status_path.is_file():
            continue
        try:
            spec = yaml.safe_load(spec_path.read_text()) or {}
        except (OSError, yaml.YAMLError):
            continue
        if spec.get("kind") != "driver":
            continue
        try:
            status = status_path.read_text().strip()
        except OSError:
            continue
        if status != "running":
            continue
        status_path.write_text("stopped\n")
        try:
            hm = datetime.now().strftime("%H:%M")
            with (child / "log.md").open("a") as f:
                f.write(f"[{hm}] boot: cleared stale running status\n")
        except OSError:
            pass


def run() -> None:
    _wipe_dir_contents(paths.PAI_ROOT / "tmp")
    _wipe_dir_contents(paths.EVENTS_DIR)
    _wipe_busy_flags()
    _reset_stale_driver_statuses()
    print(
        "[boot] clean: wiped tmp/, run/pai/events/, stale busy flags, "
        "and stale driver statuses",
        flush=True,
    )
