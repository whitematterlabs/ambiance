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


def read_visible_to(skill_md: Path) -> Optional[list]:
    """Parse `visible_to:` from the SKILL.md frontmatter.

    Returns None when the file lacks the field (= public). Returns the
    parsed list otherwise. Malformed frontmatter is treated as public —
    we don't want a typo to silently hide a skill from everyone.
    """
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
        meta = yaml.safe_load(text[3:end]) or {}
    except yaml.YAMLError:
        return None
    raw = meta.get("visible_to")
    if raw is None:
        return None
    if not isinstance(raw, list):
        return None
    return raw


def is_visible(skill_md: Path, pai_slug: str, pai_pid: int) -> bool:
    """Match a SKILL.md's `visible_to` against this PAI's slug or pid."""
    visible = read_visible_to(skill_md)
    if visible is None:
        return True
    return pai_slug in visible or pai_pid in visible
