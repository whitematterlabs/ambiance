"""Phase 4: reconcile — apply /etc/config.yaml against the fleet."""
from __future__ import annotations

from .. import config


def run() -> None:
    config.reconcile_from_config()
    print("[boot] reconcile: fleet reconciled", flush=True)
