"""Shared SKILL.md frontmatter parser.

Used by stitch (to filter the per-PAI memory/skills/ view) and by
bootstrap (to filter the <system-skills> block in the prompt).

A skill marks itself root-only — or any-other-restricted-set — by
listing slugs (or pids) under `visible_to:` in its frontmatter. No
field at all means visible to every PAI.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import yaml


def _read_frontmatter(skill_md: Path) -> Optional[dict]:
    """Parse the YAML frontmatter at the top of a SKILL.md, or None."""
    try:
        text = skill_md.read_text()
    except OSError:
        return None
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end == -1:
        return None
    try:
        meta = yaml.safe_load(text[3:end])
    except yaml.YAMLError:
        return None
    if not isinstance(meta, dict):
        return None
    return meta


def read_visible_to(skill_md: Path) -> Optional[list]:
    """Parse `visible_to:` from the SKILL.md frontmatter.

    Returns None when the file lacks the field (= public). Returns the
    parsed list otherwise. Malformed frontmatter is treated as public —
    we don't want a typo to silently hide a skill from everyone.
    """
    meta = _read_frontmatter(skill_md) or {}
    raw = meta.get("visible_to")
    if raw is None or not isinstance(raw, list):
        return None
    return raw


def read_driver_binding(skill_md: Path) -> Optional[str]:
    """Parse `driver:` from the SKILL.md frontmatter, or None if unset.

    A skill that names a driver is only visible to PAIs that mount that
    driver — see `is_visible` for the rule.
    """
    meta = _read_frontmatter(skill_md) or {}
    raw = meta.get("driver")
    if isinstance(raw, str) and raw:
        return raw
    return None


def is_visible(
    skill_md: Path,
    pai_slug: str,
    pai_pid: int,
    mounted_drivers: Optional[set[str]] = None,
) -> bool:
    """Match a SKILL.md against this PAI's slug/pid and mounted drivers.

    Precedence: a `driver:` binding (when set) gates visibility on whether
    the PAI mounts that driver. Otherwise fall back to `visible_to:`.
    """
    driver = read_driver_binding(skill_md)
    if driver is not None:
        return driver in (mounted_drivers or set())
    visible = read_visible_to(skill_md)
    if visible is None:
        return True
    return pai_slug in visible or pai_pid in visible
