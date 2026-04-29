"""Phase 1: sanity — verify required dirs exist; bail loudly if not."""
from __future__ import annotations

from .. import paths

REQUIRED: tuple[str, ...] = (
    "etc",
    "var/lib",
    "var/log",
    "proc",
    "run",
    "boot",
    "usr",
)


class SanityError(RuntimeError):
    pass


def run() -> None:
    missing = [rel for rel in REQUIRED if not (paths.PAI_ROOT / rel).exists()]
    if missing:
        raise SanityError(
            f"PAI_ROOT={paths.PAI_ROOT} missing: {', '.join(missing)}"
        )
    print(f"[boot] sanity: layout OK at {paths.PAI_ROOT}", flush=True)
