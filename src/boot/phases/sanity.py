"""Phase 1: sanity — verify required dirs exist; bail loudly if not."""
from __future__ import annotations

# Import the module, not the name: PAI_ROOT is resolved at import time
# from os.environ. Tests reload this module after monkeypatching PAI_ROOT;
# a `from ..paths import PAI_ROOT` would capture the pre-reload value.
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
