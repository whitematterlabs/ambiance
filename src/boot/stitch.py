"""Stitch a PAI's home — build the symlink view per v3 FILESYSTEM spec.

A PAI's home is a directory of symlinks pointing into:
  - the PAI's own instance state at /var/lib/instances/<slug>/
  - the canonical shared memory at /var/lib/memory/

The "root" PAI (pid 1) lives at /root/ — same shape, different slot.
Every other PAI lives at /home/<slug>/.

Stitch is idempotent: re-running heals broken/missing links. Existing
instance content is never overwritten.
"""

from __future__ import annotations

from pathlib import Path

from . import paths


def _stitch_links(home: Path, instance: Path) -> None:
    # Symlink targets are relative so the home tree is portable if PAI_ROOT moves.
    # /root/ → 1 level up; /home/<slug>/ → 2 levels up.
    depth = len(home.relative_to(paths.PAI_ROOT).parts)
    up = Path(*[".."] * depth)
    inst_rel = up / instance.relative_to(paths.PAI_ROOT)
    mem_rel = up / paths.var_lib_memory().relative_to(paths.PAI_ROOT)
    skills_rel = up / paths.usr_lib_skills().relative_to(paths.PAI_ROOT)

    links: tuple[tuple[str, Path], ...] = (
        ("inbox", inst_rel / "inbox"),
        ("workspace", inst_rel / "workspace"),
        ("memory/private", inst_rel / "memory" / "private"),
        ("memory/shared", mem_rel),
        ("memory/skills", skills_rel),
    )
    for rel, target in links:
        link = home / rel
        link.parent.mkdir(parents=True, exist_ok=True)
        if link.is_symlink():
            if link.readlink() == target:
                continue
            link.unlink()
        elif link.exists():
            if link.is_dir() and not any(link.iterdir()):
                link.rmdir()
            else:
                continue
        link.symlink_to(target)
    (home / "tmp").mkdir(exist_ok=True)


def _seed_instance(instance: Path) -> None:
    """Ensure the instance state dirs exist. Never overwrites."""
    instance.mkdir(parents=True, exist_ok=True)
    for sub in ("inbox", "workspace", "memory/private"):
        (instance / sub).mkdir(parents=True, exist_ok=True)


def home_for(slug: str) -> Path:
    """The pid-1 PAI lives at /root/; everyone else at /home/<slug>/."""
    return paths.root_home() if slug == "root" else paths.home_pai(slug)


def stitch_home(slug: str) -> Path:
    """Build (or heal) the home tree for `slug`. Returns the home path."""
    instance = paths.var_lib_instance(slug)
    home = home_for(slug)
    _seed_instance(instance)
    home.mkdir(parents=True, exist_ok=True)
    _stitch_links(home, instance)
    return home
