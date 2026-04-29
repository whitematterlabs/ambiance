"""Phase 2: clean — wipe ephemeral state from prior boots.

`tmp/` is system-wide ephemeral. `run/pai/events/` may hold stale event
files dropped by drivers between the kernel's last shutdown and this
boot. We do NOT wipe `proc/` here — process state is owned by the
proc-layer migration. Stale `proc/<pid>/` cleanup belongs to the
follow-up plan that introduces PID-keyed proc.
"""
from __future__ import annotations

import shutil

# Import the module, not the name: PAI_ROOT is resolved at import time
# from os.environ. Tests reload boot.paths after monkeypatching PAI_ROOT;
# a `from ..paths import PAI_ROOT` would capture the pre-reload value.
from .. import paths


def _wipe_dir_contents(path) -> None:
    if not path.is_dir():
        return
    for child in path.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()


def run() -> None:
    _wipe_dir_contents(paths.PAI_ROOT / "tmp")
    _wipe_dir_contents(paths.PAI_ROOT / "run" / "pai" / "events")
    print("[boot] clean: wiped tmp/ and run/pai/events/", flush=True)
