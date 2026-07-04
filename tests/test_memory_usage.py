from __future__ import annotations

from pathlib import Path

from bin import paifs_init
from boot import stitch


def test_memory_usage_routes_durable_writes_to_librarian() -> None:
    repo = Path(__file__).resolve().parents[1]
    text = (repo / "src" / "etc" / "boilerplate" / "memory-usage.md").read_text()

    # Single write path through the librarian, no direct edits.
    assert "There is one write path: `memorize`" in text
    assert "librarian" in text
    assert "Never edit memory files yourself" in text
    assert "report that memory storage failed" in text
    # When to call it.
    assert "`memorize` when you learn a durable fact" in text
    assert "Before ending a turn, ask whether you learned something" in text
    assert "without waiting for \"remember this.\"" in text
    assert "owner preferences or corrections" in text
    assert "capability/routing discoveries" in text
    # Private variant + read paths.
    assert "`--private` is for classified or very sensitive info" in text
    assert "cross-contaminate PAIs" in text
    assert "`remember '<question>'`" in text
    assert "read-only lookup to `librarian`" in text
    # Deduped away: no shared flag, no raw redirection, no path enumeration.
    assert "memorize --shared" not in text
    assert ">> memory/" not in text
    assert "### No direct journals" not in text
    assert "### You do not write to" not in text


def test_memory_tools_seeded_for_fresh_roots() -> None:
    assert "memorize" in paifs_init.KERNEL_SEED_BINS
    assert "remember" in paifs_init.KERNEL_SEED_BINS


def test_owner_onboarding_tools_seeded_for_fresh_roots() -> None:
    assert "inbox" in paifs_init.KERNEL_SEED_BINS
    assert "imessage-history" in paifs_init.KERNEL_SEED_BINS


def test_owner_onboarding_skill_seeded_for_fresh_roots() -> None:
    assert "onboard-owner" in paifs_init.KERNEL_SEED_SKILLS


def test_private_memory_seed_header_does_not_invite_direct_edits() -> None:
    assert "Owned by librarian" in stitch._PRIVATE_MEMORY_INDEX_HEADER
    assert "You write here" not in stitch._PRIVATE_MEMORY_INDEX_HEADER
