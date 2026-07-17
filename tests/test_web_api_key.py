"""Provider API-key entry from the web console (POST /api/apikey backing).

set_api_key persists to $PAI_ROOT/.env (chmod 600), goes live in this process,
and emits kernel:reload_config so the kernel re-reads .env (boot.reload_env).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from usr.libexec.web.pai_web import actions


@pytest.fixture
def env_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(actions.paths, "PAI_ROOT", tmp_path)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    return tmp_path


@pytest.fixture
def events(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    sent: list[dict] = []
    monkeypatch.setattr(actions, "emit_event", sent.append)
    return sent


def test_set_api_key_persists_and_reloads(env_root: Path, events: list[dict]) -> None:
    out = actions.set_api_key("openrouter", "  sk-or-abcdef123456  ")
    assert out == {"provider": "openrouter", "key_status": "found"}
    assert os.environ["OPENROUTER_API_KEY"] == "sk-or-abcdef123456"
    env_file = env_root / ".env"
    assert "sk-or-abcdef123456" in env_file.read_text()
    assert (env_file.stat().st_mode & 0o777) == 0o600
    assert len(events) == 1
    assert events[0]["kind"] == "kernel:reload_config"
    assert events[0]["provider"] == "openrouter"
    assert "key" not in events[0]  # the secret never rides an event file


def test_set_gemini_api_key_persists_to_gemini_env(env_root: Path, events: list[dict]) -> None:
    out = actions.set_api_key("gemini", "  gemini-test-key  ")
    assert out == {"provider": "gemini", "key_status": "found"}
    assert os.environ["GEMINI_API_KEY"] == "gemini-test-key"
    assert "GEMINI_API_KEY" in (env_root / ".env").read_text()
    assert "gemini-test-key" in (env_root / ".env").read_text()
    assert events[0]["provider"] == "gemini"
    assert "key" not in events[0]


def test_set_api_key_unknown_provider(env_root: Path, events: list[dict]) -> None:
    with pytest.raises(ValueError, match="unknown provider"):
        actions.set_api_key("grok", "sk-whatever")
    assert not (env_root / ".env").exists()
    assert events == []


def test_set_api_key_rejects_empty_and_whitespace(env_root: Path, events: list[dict]) -> None:
    with pytest.raises(ValueError):
        actions.set_api_key("openrouter", "   ")
    with pytest.raises(ValueError):
        actions.set_api_key("openrouter", "sk bad")
    assert events == []


def test_dotenv_lookup_resolution_order(env_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert actions._dotenv_lookup("OPENROUTER_API_KEY") is None
    (env_root / ".env").write_text("OPENROUTER_API_KEY=from_env_file\n")
    assert actions._dotenv_lookup("OPENROUTER_API_KEY") == "from_env_file"
    (env_root / ".env.local").write_text("OPENROUTER_API_KEY=from_local\n")
    assert actions._dotenv_lookup("OPENROUTER_API_KEY") == "from_local"
    monkeypatch.setenv("OPENROUTER_API_KEY", "from_process")
    assert actions._dotenv_lookup("OPENROUTER_API_KEY") == "from_process"
