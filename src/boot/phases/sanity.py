"""Phase 1: sanity — verify required dirs exist; bail loudly if not."""
from __future__ import annotations

# Import the module, not the name: PAI_ROOT is resolved at import time
# from os.environ. Tests reload this module after monkeypatching PAI_ROOT;
# a `from ..paths import PAI_ROOT` would capture the pre-reload value.
from .. import paths

# State dirs that must exist for the kernel to run. Deliberately NOT including
# code slots (boot, usr/src): the kernel's code is resolved by Python import —
# from the repo via the dev .pth symlinks, or from the embedded interpreter's
# site-packages in a bundled PAI.app, which has no $PAI_ROOT/boot dir. If the
# code were missing the kernel wouldn't reach this phase (it'd fail at
# `import boot.entry`), so checking for the dir is both redundant and wrong for
# the bundle layout. See src/bin/paifs_init.py bundle mode.
REQUIRED: tuple[str, ...] = (
    "etc",
    "var/lib",
    "var/log",
    "proc",
    "run",
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
