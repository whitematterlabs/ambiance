"""claude_backend.run_turn — quiet-turn sentinel canonicalization."""

from __future__ import annotations

import asyncio

import pytest

from boot import claude_backend


@pytest.fixture
def fake_invoke(monkeypatch: pytest.MonkeyPatch):
    """Stub every external edge of run_turn; the test picks the CLI result."""
    data = {"result": "", "is_error": False, "session_id": None, "usage": None}
    monkeypatch.setattr(claude_backend, "_auth_env", lambda: {"ANTHROPIC_API_KEY": "k"})
    monkeypatch.setattr(claude_backend, "_claude_bin", lambda: "/bin/claude")
    monkeypatch.setattr(claude_backend, "_read_session", lambda slug: None)
    monkeypatch.setattr(claude_backend, "_write_session", lambda slug, sid: None)

    async def _invoke(exe, args, user, home, child_env):
        return data

    monkeypatch.setattr(claude_backend, "_invoke", _invoke)
    return data


@pytest.mark.parametrize("prose", ["do_nothing", "Quiet.", "no update", "NOOP"])
def test_sentinel_prose_canonicalized_to_no_reply(fake_invoke, prose):
    fake_invoke["result"] = prose
    reply, messages = asyncio.run(
        claude_backend.run_turn("sys", [], "nudge", env={"PAI_SLUG": "test"})
    )
    assert reply == ""
    assert messages[-1] == {"role": "assistant", "content": ""}


def test_real_reply_passes_through(fake_invoke):
    fake_invoke["result"] = "Done — archived the thread."
    reply, _ = asyncio.run(
        claude_backend.run_turn("sys", [], "nudge", env={"PAI_SLUG": "test"})
    )
    assert reply == "Done — archived the thread."
