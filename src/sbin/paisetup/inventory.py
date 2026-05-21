"""Discover registry packages and which are already installed."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from boot import paths
from bin import paiman
from bin.paifs_init import (
    KERNEL_SEED_BINS,
    KERNEL_SEED_DRIVERS,
    KERNEL_SEED_PAIS,
    KERNEL_SEED_SKILLS,
)

# Packages the wizard hides:
#   - kernel seeds (installed by paifs-init, required for boot)
#   - root-only skills (kernel authoring/diagnosing/ops — not for owner PAIs)
_ROOT_ONLY_SKILLS: frozenset[str] = frozenset({
    # authoring/
    "author-driver", "author-pai-bundle", "author-plan", "author-skill",
    "manage-subagent-bundles",
    # diagnosing/
    "diagnose-crash", "inspect-fleet", "reload-config", "restart-driver",
    # operating/ (root-only subset)
    "kernel-restart", "kernel-tools", "manage-dependencies",
    "execute-claudecode", "execute-plan",
})

_HIDDEN: dict[str, frozenset[str]] = {
    "driver": frozenset(KERNEL_SEED_DRIVERS),
    "skill": frozenset(KERNEL_SEED_SKILLS) | _ROOT_ONLY_SKILLS,
    "bin": frozenset(KERNEL_SEED_BINS),
    "pai": frozenset(KERNEL_SEED_PAIS),
}


@dataclass
class Item:
    kind: str           # "driver", "skill", "pai", "subagent"
    name: str
    description: str
    installed: bool
    # On-disk source path in the (possibly cloned) registry. We pass this
    # to `paiman install` instead of the bare name so a name shared across
    # kinds (e.g. bin/browse + subagents/browse) resolves unambiguously.
    source: str = ""
    # Registry-relative typed ref, e.g. "subagents/browse". Survives tempdir
    # cleanup (pure path string) so a URL-cloned registry can still install
    # the exact package by `paiman install <ref>` even when names collide
    # across kinds. See _Registry.lookup's typed `<topic>/<name>` form.
    ref: str = ""


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
    for name, data, path in bundles:
        kind = data.get("kind")
        if kind not in groups:
            continue
        if name in _HIDDEN.get(kind, frozenset()):
            continue
        desc = (data.get("description") or "").strip()
        try:
            ref = str(path.relative_to(root))
        except ValueError:
            ref = name
        groups[kind].append(
            Item(
                kind=kind,
                name=name,
                description=desc,
                installed=_is_installed(kind, name),
                source=str(path),
                ref=ref,
            )
        )
    for k in groups:
        groups[k].sort(key=lambda i: i.name)
    return groups
