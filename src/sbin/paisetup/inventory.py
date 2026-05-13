"""Discover registry packages and which are already installed."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from boot import paths
from bin import paiman
from bin.paifs_init import KERNEL_SEED_DRIVERS, KERNEL_SEED_SKILLS

# Kernel-seeded packages are installed by paifs-init and required for the
# kernel to import cleanly. They're not user-pickable, so paisetup hides them.
_HIDDEN: dict[str, frozenset[str]] = {
    "driver": frozenset(KERNEL_SEED_DRIVERS),
    "skill": frozenset(KERNEL_SEED_SKILLS),
}


@dataclass
class Item:
    kind: str           # "driver", "skill", "pai", "subagent"
    name: str
    description: str
    installed: bool


# Map registry-kind (from package.yaml `kind:`) to the FS slot we check
# to decide if it's already installed. Mirrors paiman._activation_slot.
_INSTALLED_SLOT = {
    "driver": paths.usr_lib_drivers,
    "skill": paths.usr_lib_skills,
    "pai": paths.usr_lib_pais,
    "subagent": paths.usr_lib_subagents,
}


def _is_installed(kind: str, name: str) -> bool:
    slot_fn = _INSTALLED_SLOT.get(kind)
    if slot_fn is None:
        return False
    p = slot_fn() / name
    return p.exists() or p.is_symlink()


def discover() -> dict[str, list[Item]]:
    """Walk the registry and return items grouped by kind, in the order
    we want to render: drivers, skills, pais, subagents."""
    with tempfile.TemporaryDirectory(prefix="paisetup-") as tmp:
        registry = paiman._Registry(Path(tmp))
        root = registry.root()
        bundles = paiman._iter_registry(root)

    groups: dict[str, list[Item]] = {
        "driver": [],
        "skill": [],
        "pai": [],
        "subagent": [],
    }
    for name, data, _path in bundles:
        kind = data.get("kind")
        if kind not in groups:
            continue
        if name in _HIDDEN.get(kind, frozenset()):
            continue
        desc = (data.get("description") or "").strip()
        groups[kind].append(
            Item(kind=kind, name=name, description=desc, installed=_is_installed(kind, name))
        )
    for k in groups:
        groups[k].sort(key=lambda i: i.name)
    return groups
