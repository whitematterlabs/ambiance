from __future__ import annotations

from pathlib import Path

from bin import paifs_init
from boot import stitch


def test_memory_usage_routes_durable_writes_to_librarian() -> None:
    repo = Path(__file__).resolve().parents[1]
    text = (repo / "src" / "etc" / "boilerplate" / "memory-usage.md").read_text()

    assert "call `memorize`" in text
    assert "librarian-pai" in text
    assert "There is one memory write path for you: `memorize`" in text
    assert "Before ending a turn, ask: did I learn anything" in text
    assert "Do not wait for the owner to say \"remember this.\"" in text
    assert "After you successfully fulfill an owner request" in text
    assert "short note of what changed or what you did" in text
    assert "Do not memorize routine one-off completions" in text
    assert "owner preferences or corrections" in text
    assert "capability/routing discoveries" in text
    assert "do not fall back to editing topic files or `MEMORY.md` yourself" in text
    assert "Plain `memorize` is the default for durable facts" in text
    assert "Reserve `memorize --private` for classified or very sensitive information" in text
    assert "avoid cross-contamination across PAIs" in text
    assert "memorize --shared" not in text
    assert "Use `remember '<question>'`" in text
    assert "read-only lookup to `librarian-pai`" in text
    assert "- `memory/private/topics/`" in text
    assert "- `memory/private/MEMORY.md`" in text
    assert "- `memory/shared/journal/`" in text
    assert "- `memory/private/journal/`" in text
    assert ">> memory/" not in text
    assert "Append one timestamped line" not in text
    assert "you write it" not in text
    assert "Your `memory/private/MEMORY.md` is yours to maintain" not in text


def test_memory_tools_seeded_for_fresh_roots() -> None:
    assert "memorize" in paifs_init.KERNEL_SEED_BINS
    assert "remember" in paifs_init.KERNEL_SEED_BINS


def test_owner_onboarding_tools_seeded_for_fresh_roots() -> None:
    assert "mailsearch" in paifs_init.KERNEL_SEED_BINS
    assert "imessage-history" in paifs_init.KERNEL_SEED_BINS


def test_owner_onboarding_skill_seeded_for_fresh_roots() -> None:
    assert "onboard-owner" in paifs_init.KERNEL_SEED_SKILLS


def test_private_memory_seed_header_does_not_invite_direct_edits() -> None:
    assert "Owned by librarian-pai" in stitch._PRIVATE_MEMORY_INDEX_HEADER
    assert "You write here" not in stitch._PRIVATE_MEMORY_INDEX_HEADER
