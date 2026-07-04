"""Phase 4: reconcile — apply /etc/config.yaml against the fleet."""
from __future__ import annotations

from .. import config, paths, processes, stitch


def _touch_me_thread(slug: str) -> None:
    # Keyed by slug, not pid — see paths.me_thread_dir.
    p = paths.me_thread_today(slug)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.touch(exist_ok=True)


def run() -> None:
    config.reconcile_from_config()
    print("[boot] reconcile: fleet reconciled", flush=True)
    # Stitch every fleet member's home view. Idempotent — re-runs heal
    # broken/missing links without clobbering instance state.
    for slug in config.load_config():
        stitch.stitch_home(slug)
    # Persubs (children declared under a parent's `dependencies:`) live in
    # /proc but not in the top-level config; stitch them by walking /proc.
    for slug, spec in processes._iter_pai_specs():
        if spec.get("persub"):
            stitch.stitch_home(slug)
    print("[boot] reconcile: homes stitched", flush=True)
    # Ensure today's me-thread day-file exists for every fleet PAI so
    # `cat communication/messages/me/<slug>/<today>.md` never has to fall
    # through to a "no file yet" branch.
    for slug in config.load_config():
        _touch_me_thread(slug)
    # Persubs are owner-addressable too (TUI opens a tab per persub) —
    # ensure their day-file exists so the TUI watcher's first read
    # doesn't fall through to the no-file branch.
    for slug, spec in processes._iter_pai_specs():
        if spec.get("persub"):
            _touch_me_thread(slug)
