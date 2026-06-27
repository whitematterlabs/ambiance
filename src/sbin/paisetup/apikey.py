"""API-key dialogue for the guided installer.

install.sh asks for the chosen provider's API key in bash, but that prompt only
fires on the `curl … | sh` path. Every other route into the guided setup —
`paifs-init` chaining into paisetup on a dev box, a re-run of `paisetup`, the
future GUI catalog — reached the package picker without ever asking for a key,
so the fleet booted keyless and failed silently on the first turn.

This module owns the key prompt inside paisetup so any path that reaches the
guided installer asks for it. It is idempotent and mirrors install.sh's
`ensure_api_key`: it only prompts when the seeded provider's key is missing from
both the environment and `$PAI_ROOT/.env{,.local}`, and writes to
`$PAI_ROOT/.env` (chmod 600) — the precedence-1 location boot/__init__.py reads.
When install.sh already captured the key, the present-key check short-circuits
and the user is never asked twice.
"""

from __future__ import annotations

import getpass
import os
import sys
from pathlib import Path

import yaml

# Single source of truth for provider -> api-key env var.
from boot.llm import PROVIDERS


def _seeded_provider(root: Path) -> str | None:
    """Read the provider the seed config.yaml boots the fleet on.

    Mirrors install.sh's first-`provider:` heuristic: the fleet is seeded with a
    single provider across all PAIs, so the first one is the fleet default.
    """
    cfg = root / "etc" / "config.yaml"
    if not cfg.exists():
        return None
    try:
        data = yaml.safe_load(cfg.read_text()) or {}
    except yaml.YAMLError:
        return None
    for pai in data.get("pais") or []:
        if isinstance(pai, dict) and pai.get("provider"):
            return str(pai["provider"])
    return None


def _key_already_present(root: Path, var: str) -> str | None:
    """Return a human note if `var` is already reachable, else None."""
    if os.environ.get(var):
        return "environment"
    for fname in (".env.local", ".env"):
        f = root / fname
        if f.exists():
            try:
                for line in f.read_text().splitlines():
                    if line.startswith(f"{var}="):
                        return str(f)
            except OSError:
                continue
    return None


def ensure_api_key(root: Path) -> None:
    """Prompt for (and store) the seeded provider's API key if it's missing.

    No-op when the provider is unknown, the key is already reachable, or the
    shell is non-interactive (the kernel surfaces the missing key at boot)."""
    provider = _seeded_provider(root)
    if not provider:
        return
    spec = PROVIDERS.get(provider)
    if spec is None:
        return
    var = spec.api_key_env

    where = _key_already_present(root, var)
    if where is not None:
        print(f"==> API key\n    {var} found in {where} — not asked.")
        return

    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print(f"==> API key\n    warning: {var} not set. "
              f"Add it to {root / '.env'} before starting PAI.", file=sys.stderr)
        return

    print(f"\n==> API key for provider {provider!r}")
    try:
        key = getpass.getpass(f"Enter {var} (input hidden, leave blank to skip): ")
    except (EOFError, KeyboardInterrupt):
        print()
        key = ""
    if not key.strip():
        print(f"    skipped — add {var} to {root / '.env'} before starting PAI.",
              file=sys.stderr)
        return

    env_file = root / ".env"
    root.mkdir(parents=True, exist_ok=True)
    with env_file.open("a", encoding="utf-8") as fh:
        fh.write(f"{var}={key.strip()}\n")
    try:
        env_file.chmod(0o600)
    except OSError:
        pass
    print(f"    saved {var} to {env_file}")
