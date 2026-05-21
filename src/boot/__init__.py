"""PAI boot — process management for the home/ filesystem."""

# Load .env before anything else imports — the anthropic client reads
# ANTHROPIC_API_KEY at construction time. Two locations, in precedence order:
#   1. $PAI_ROOT (runtime state) — the ONLY source a Finder-launched .app has.
#      Its code root is the bundle's site-packages (no .env there) and the GUI
#      process inherits no shell env, so this is where the key must live.
#   2. the code root (dev convenience) — the repo checkout's .env, fallback.
# dotenv's override=False means the first file to set a var wins, so load the
# higher-precedence location first. Within a base, .env.local beats .env.
import os as _os
from pathlib import Path as _Path

from dotenv import load_dotenv as _load_dotenv

_pai_root = _Path(_os.environ.get("PAI_ROOT", str(_Path.home() / ".pai")))
_code_root = _Path(__file__).resolve().parent.parent.parent
for _base in (_pai_root, _code_root):
    _load_dotenv(_base / ".env.local")
    _load_dotenv(_base / ".env")
