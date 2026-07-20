"""Member config — this agent's own entry in /etc/pai/config.yaml.

The file is root-owned team policy; the agent only ever reads its own
`members.<user>` mapping. Fleet lifecycle is systemd's (`useradd` +
`systemctl enable pai@<user>`), capability policy is the broker's — the
agent has no reconcile, no write path, no view of other members beyond
the roster names.

Shape:

    members:
      john:
        provider: anthropic          # anthropic | deepseek | zai
        model: claude-sonnet-4-6
        prompt: member.md            # in /usr/lib/pai/prompts/, or absolute
        compact_threshold: 150000
        hard_compact_threshold: 400000
    capabilities: { ... }            # broker-owned; opaque here

Tolerant read: a missing file or absent entry boots the agent on
defaults — a provisioned Unix user with a unit is a valid member.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from . import paths


def _load_yaml(path: Path) -> dict:
    try:
        with path.open() as f:
            data = yaml.safe_load(f)
    except (OSError, yaml.YAMLError) as e:
        print(f"agent: config unreadable ({e}) — using defaults", flush=True)
        return {}
    return data if isinstance(data, dict) else {}


def member_entry(user: str, path: Path | None = None) -> dict[str, Any]:
    data = _load_yaml(path or paths.CONFIG)
    members = data.get("members")
    if not isinstance(members, dict):
        return {}
    entry = members.get(user)
    return entry if isinstance(entry, dict) else {}


def roster(path: Path | None = None) -> list[str]:
    """Every configured member name — the reachable-by-spool team list."""
    data = _load_yaml(path or paths.CONFIG)
    members = data.get("members")
    if not isinstance(members, dict):
        return []
    return sorted(k for k in members if isinstance(k, str))
