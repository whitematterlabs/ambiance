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


def test_usage_recorded_per_iteration(fake_invoke, monkeypatch):
    """The turn aggregate must not become last_window_tokens — record each
    messages.create iteration so the window gauge reads the real context."""
    recorded = []
    monkeypatch.setattr(
        claude_backend.tokens, "record", lambda slug, model, u: recorded.append(u)
    )
    it1 = {"type": "message", "input_tokens": 2, "output_tokens": 50,
           "cache_read_input_tokens": 55_000, "cache_creation_input_tokens": 300}
    it2 = {"type": "message", "input_tokens": 2, "output_tokens": 20,
           "cache_read_input_tokens": 55_300, "cache_creation_input_tokens": 200}
    fake_invoke["result"] = "ok"
    fake_invoke["usage"] = {
        "input_tokens": 4, "output_tokens": 70,
        "cache_read_input_tokens": 110_300, "cache_creation_input_tokens": 500,
        "iterations": [it1, it2],
    }
    asyncio.run(claude_backend.run_turn("sys", [], "nudge", env={"PAI_SLUG": "test"}))
    assert recorded == [it1, it2]


def test_usage_without_iterations_falls_back_to_aggregate(fake_invoke, monkeypatch):
    recorded = []
    monkeypatch.setattr(
        claude_backend.tokens, "record", lambda slug, model, u: recorded.append(u)
    )
    fake_invoke["result"] = "ok"
    fake_invoke["usage"] = {"input_tokens": 2, "output_tokens": 10,
                            "cache_read_input_tokens": 100, "cache_creation_input_tokens": 5}
    asyncio.run(claude_backend.run_turn("sys", [], "nudge", env={"PAI_SLUG": "test"}))
    assert recorded == [fake_invoke["usage"]]
