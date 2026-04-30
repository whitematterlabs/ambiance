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
    # Each link's `..`-prefix is computed from the link's *own* depth under
    # PAI_ROOT (not the home's), so a link nested under `memory/` adds one
    # more `..` than a top-level link in the home.
    home_under_root = home.relative_to(paths.PAI_ROOT)
    inst_under_root = instance.relative_to(paths.PAI_ROOT)
    mem_under_root = paths.var_lib_memory().relative_to(paths.PAI_ROOT)
    skills_under_root = paths.usr_lib_skills().relative_to(paths.PAI_ROOT)
    doc_under_root = paths.usr_share_doc().relative_to(paths.PAI_ROOT)

    links: tuple[tuple[str, Path], ...] = (
        ("inbox", inst_under_root / "inbox"),
        ("workspace", inst_under_root / "workspace"),
        ("memory/private", inst_under_root / "memory" / "private"),
        ("memory/shared", mem_under_root),
        ("memory/skills", skills_under_root),
        ("memory/doc", doc_under_root),
    )
    for rel, target_under_root in links:
        link = home / rel
        link.parent.mkdir(parents=True, exist_ok=True)
        # Depth of the link's parent dir under PAI_ROOT — that's how many
        # `..` segments are needed to climb back to PAI_ROOT.
        link_parent_depth = len(home_under_root.parts) + rel.count("/")
        target = Path(*[".."] * link_parent_depth) / target_under_root
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
