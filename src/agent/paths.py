"""v4 filesystem map — literal paths; the OS FHS is the FHS.

There is no PAI_ROOT, no prefix env var, no rewriting. System paths are
namespaced under a `pai/` segment the way any daemon's are (postfix
style); member state lives plainly in the member's real home, where DAC
already enforces the boundary. Code that finds these paths missing is
running on an unprovisioned box — that is an error to surface, never a
layout to emulate.
"""

from __future__ import annotations

import os
import pwd
from pathlib import Path

# System plane — root-owned, laid down by the image, sealed against edits.
ETC = Path("/etc/pai")
CONFIG = ETC / "config.yaml"  # per-member settings + capability policy
USR_LIB = Path("/usr/lib/pai")  # sealed release tree: venv + this package
PROMPTS = USR_LIB / "prompts"  # root-owned base personas
VAR_LIB = Path("/var/lib/pai")
MEMORY = VAR_LIB / "memory"  # team hivemind (root:org, setgid)
DEALS = MEMORY / "deals"  # walled subtrees (root:deal-<slug>)
SPOOL = Path("/var/spool/pai")  # per-member inboxes + shared comms archive
RUN = Path("/run/pai")  # broker.sock + per-member run dirs
LOG = Path("/var/log/pai")  # audit.log (broker-owned); process logs are journald's


def member() -> str:
    """The member this process serves — simply the uid it runs as."""
    return pwd.getpwuid(os.getuid()).pw_name


def home(user: str) -> Path:
    return Path(pwd.getpwnam(user).pw_dir)


def inbox(user: str) -> Path:
    """The member's inbox spool — the inotify wake source. Anything that
    wants this member's attention (another member's PAI, a scheduled
    hand-off, later a driver) delivers by dropping a file here."""
    return SPOOL / user / "in"


def rundir(user: str) -> Path:
    return RUN / user


def api_sock(user: str) -> Path:
    """The member's console API socket (slot fixed by the spec; serving it
    is deferred with the console re-plumb)."""
    return rundir(user) / "api.sock"


# Member plane — inside ~, owned by the member, 0700 at the home boundary.
def private_memory(user: str) -> Path:
    return home(user) / "memory"


def prompt_overlay(user: str) -> Path:
    return home(user) / "prompt"


def state(user: str) -> Path:
    """Machine state (session history, cursors) — XDG state dir, kept out
    of the member's visible working tree."""
    return home(user) / ".local" / "state" / "pai"
