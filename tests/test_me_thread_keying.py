"""The owner-facing me/ display thread must be keyed by a PAI's unique slug,
never by its pid. Pids are small integers reused across reboots and subagents;
keying the on-disk transcript by pid replays a prior process's conversation
into a freshly-started one (the real LLM context, proc/<slug>/messages.jsonl,
is already slug-keyed and stays empty — hence the "ctx 0 but old messages"
symptom this guards against)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from boot import nudge as N
from boot import paths as PA
from boot import processes as P


@pytest.fixture(autouse=True)
def _reset(live_dir: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(N, "HOME_DIR", PA.HOME_DIR, raising=True)
    monkeypatch.setattr(N, "PROC_DIR", P.PROC_DIR, raising=True)


def _read(slug: str) -> str:
    path = PA.me_thread_today(slug)
    return path.read_text() if path.exists() else ""


def test_writer_keys_by_slug_so_pid_reuse_does_not_collide() -> None:
    # Two different PAIs that happen to reuse the same pid write to distinct
    # transcripts — the whole point of keying by identity.
    N._append_to_me_thread("alpha", "old night message")
    N._append_to_me_thread("beta", "fresh clone message")

    assert "old night message" in _read("alpha")
    assert "old night message" not in _read("beta")
    assert "fresh clone message" in _read("beta")


def test_paths_helper_is_per_slug_per_day() -> None:
    day = date.today().isoformat()
    assert PA.me_thread_today("alpha") == PA.me_thread_dir("alpha") / f"{day}.md"
    assert PA.me_thread_dir("alpha") != PA.me_thread_dir("beta")


def test_slug_for_pid_resolves_running_proc() -> None:
    P.spawn_pai(pid=4, slug="alpha", description="alpha test")
    assert P.slug_for_pid(4) == "alpha"


def test_reader_reads_fresh_clones_thread_after_pid_reuse() -> None:
    from usr.libexec.web.pai_web import hub

    # A prior PAI wrote a thread; it happened to run as pid 4.
    N._append_to_me_thread("alpha", "old night message")

    # A fresh clone now owns pid 4. The reader resolves pid -> slug, so it
    # reads the clone's (empty) thread, not the recycled pid's leftovers.
    P.spawn_pai(pid=4, slug="beta", description="beta test")
    assert hub.read_thread(4) == []
