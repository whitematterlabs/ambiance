"""PAI boot — process management for the home/ filesystem."""

# Load .env from the project root before anything else imports — the
# anthropic client reads ANTHROPIC_API_KEY at construction time.
from pathlib import Path as _Path

from dotenv import load_dotenv as _load_dotenv

_root = _Path(__file__).resolve().parent.parent.parent
# .env.local takes precedence over .env (per dotenv's override=False default:
# whichever loads first wins, so load the more-specific file first).
_load_dotenv(_root / ".env.local")
_load_dotenv(_root / ".env")

from .main import run
from .processes import (
    HOME_DIR,
    PROC_DIR,
    EVENTS_DIR,
    ProcessExists,
    ProcessNotFound,
    append_log,
    emit_event,
    list_procs,
    read_spec,
    read_status,
    resolve,
    show,
    spawn,
)

__all__ = [
    "HOME_DIR",
    "PROC_DIR",
    "EVENTS_DIR",
    "ProcessExists",
    "ProcessNotFound",
    "append_log",
    "emit_event",
    "list_procs",
    "read_spec",
    "read_status",
    "resolve",
    "run",
    "show",
    "spawn",
]
