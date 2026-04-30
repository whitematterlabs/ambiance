"""Phase 4: reconcile — apply /etc/config.yaml against the fleet."""
from __future__ import annotations

from datetime import date

from .. import config, paths, processes, stitch


def _touch_me_thread(pid: int) -> None:
    day = date.today().isoformat()
    p = paths.var_spool_messages() / "me" / str(pid) / f"{day}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.touch(exist_ok=True)


def run() -> None:
    config.reconcile_from_config()
    print("[boot] reconcile: fleet reconciled", flush=True)
    # Stitch every fleet member's home view. Idempotent — re-runs heal
    # broken/missing links without clobbering instance state.
    for slug in config.load_config():
        stitch.stitch_home(slug)
    print("[boot] reconcile: homes stitched", flush=True)
    # Ensure today's me-thread day-file exists for every fleet PAI so
    # `cat communication/messages/me/<pid>/<today>.md` never has to fall
    # through to a "no file yet" branch.
    for slug in config.load_config():
        pid = processes.read_pai_pid(slug)
        if pid is not None:
            _touch_me_thread(pid)
