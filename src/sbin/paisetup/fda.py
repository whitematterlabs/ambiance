"""Full Disk Access guidance for the guided installer.

macOS gates the imessage store (~/Library/Messages/chat.db) and Mail
(~/Library/Mail) behind Full Disk Access. There is no API to *prompt* for FDA —
the OS shows no dialog — so a fresh install silently leaves those drivers idle,
and imessage even logs a misleading "chat.db not found" when the file is right
there but unreadable. Worse, the grant must target the *terminal emulator* PAI
runs under: the bundled .app was removed, so PAI borrows the host terminal's TCC
identity (see CLAUDE.md). Nothing in setup ever told the user any of this.

This step closes that gap as far as it can: when an FDA-dependent driver is
installed and access is denied, it names the host terminal, opens the Privacy
pane, and spells out what to add. It can only guide, not grant — and the grant
needs a terminal restart to take effect, which the message makes explicit.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

# Drivers whose on-disk stores macOS gates behind Full Disk Access.
FDA_DRIVERS = ("imessage", "email")

# Files readable only with Full Disk Access. A plain exists() check is useless
# here — macOS reports gated paths as absent — so we actually open them and read
# the EPERM: a successful read means FDA is granted, a PermissionError means it's
# denied. chat.db carries the signal on essentially every Mac (anyone who has
# opened Messages has one); TCC.db is the backstop.
_FDA_PROBES = (
    Path.home() / "Library" / "Messages" / "chat.db",
    Path.home() / "Library" / "Application Support" / "com.apple.TCC" / "TCC.db",
)

# $TERM_PROGRAM value -> human name of the app that owns the TCC grant.
_TERMINALS = {
    "Apple_Terminal": "Terminal",
    "iTerm.app": "iTerm",
    "vscode": "Visual Studio Code",
    "Hyper": "Hyper",
    "WezTerm": "WezTerm",
    "ghostty": "Ghostty",
    "Tabby": "Tabby",
    "Warp": "Warp",
}


def _has_full_disk_access() -> bool | None:
    """True if granted, False if denied, None if undeterminable.

    Returns True on the first protected file we can actually read, False if we
    only ever hit PermissionError, None when every probe is inconclusively
    absent (a Mac that has never run Messages and has no readable TCC.db)."""
    saw_denied = False
    for p in _FDA_PROBES:
        try:
            with p.open("rb") as fh:
                fh.read(1)
            return True
        except PermissionError:
            saw_denied = True
        except OSError:
            continue
    return False if saw_denied else None


def installed_fda_drivers(root: Path) -> list[str]:
    """FDA-gated drivers actually present on disk under this root."""
    base = root / "usr" / "lib" / "drivers"
    return [d for d in FDA_DRIVERS if (base / d).exists()]


def _host_terminal() -> str:
    return _TERMINALS.get(os.environ.get("TERM_PROGRAM", ""), "your terminal app")


def ensure_full_disk_access(root: Path) -> None:
    """Guide the user to grant FDA when an FDA-gated driver is installed.

    No-op off macOS, when no such driver is installed, or when access is already
    granted (or can't be determined — we don't nag on a maybe)."""
    if sys.platform != "darwin":
        return
    drivers = installed_fda_drivers(root)
    if not drivers:
        return
    if _has_full_disk_access() is not False:
        return

    term = _host_terminal()
    caps = " and ".join(drivers)
    print()
    print("==> Full Disk Access")
    print(f"    The {caps} driver(s) need Full Disk Access to read Messages/Mail,")
    print("    and macOS shows no prompt for it — without this they stay silently")
    print("    idle (the rest of PAI, including the web console, works regardless).")
    print()
    print(f"    1. In the window that opens, add and enable: {term}")
    print(f"    2. Fully quit and reopen {term} — the grant only applies on restart.")
    print("    3. Re-run 'pai start' (or 'paisetup') and Messages/Mail will light up.")
    try:
        subprocess.run(
            ["open",
             "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFilesAccess"],
            check=False,
        )
    except OSError:
        print("    (open it manually: System Settings → Privacy & Security →"
              " Full Disk Access)")
