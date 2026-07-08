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
import pwd as _pwd
from pathlib import Path as _Path

from dotenv import load_dotenv as _load_dotenv

_real_home = _Path(_pwd.getpwuid(_os.getuid()).pw_dir)
_pai_root = _Path(_os.environ.get("PAI_ROOT", str(_real_home / ".pai")))
_code_root = _Path(__file__).resolve().parent.parent.parent
for _base in (_pai_root, _code_root):
    _load_dotenv(_base / ".env.local")
    _load_dotenv(_base / ".env")


def reload_env() -> None:
    """Re-read the .env files so runtime edits (web-console key entry) go live.

    Boot loads with override=False, high-precedence file first. To keep the
    same precedence with override=True we load lowest-precedence first so the
    later files win. override=True means .env values now beat inherited shell
    exports — intended: the console's key editor writes .env and must win over
    a stale exported var. Called by the kernel on kernel:reload_config.
    """
    for _base in (_code_root, _pai_root):
        for _name in (".env", ".env.local"):
            _load_dotenv(_base / _name, override=True)
