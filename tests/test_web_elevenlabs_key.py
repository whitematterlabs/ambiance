"""Tests for the web-managed ElevenLabs API key (voice dropdown "API key" row).

Covers the two helpers behind /api/elevenlabs-key: `elevenlabs_key_status`
(masked status, env-then-dotenv resolution) and `set_elevenlabs_key`
(persist to $PAI_ROOT/.env(.local) + immediate process-env visibility).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from usr.libexec.web.pai_web import actions


@pytest.fixture
def env_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(actions.paths, "PAI_ROOT", tmp_path)
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    return tmp_path


def test_status_unset(env_root: Path) -> None:
    assert actions.elevenlabs_key_status() == {"set": False, "hint": None}


def test_status_reads_dotenv(env_root: Path) -> None:
    (env_root / ".env").write_text("ELEVENLABS_API_KEY=sk_abcdef123456\n")
    assert actions.elevenlabs_key_status() == {"set": True, "hint": "…3456"}


def test_status_never_returns_full_key(env_root: Path) -> None:
    # Short keys get no hint at all rather than leaking most of the secret.
    (env_root / ".env").write_text("ELEVENLABS_API_KEY=short\n")
    assert actions.elevenlabs_key_status() == {"set": True, "hint": None}


def test_set_persists_to_env_and_process(env_root: Path) -> None:
    out = actions.set_elevenlabs_key("  sk_abcdef123456  ")
    assert out == {"set": True, "hint": "…3456"}
    # Live immediately for this process (the TTS provider checks env first).
    assert os.environ["ELEVENLABS_API_KEY"] == "sk_abcdef123456"
    # Persisted for the next boot.
    assert "sk_abcdef123456" in (env_root / ".env").read_text()


def test_set_updates_env_local_when_it_defines_the_key(env_root: Path) -> None:
    # .env.local shadows .env at boot, so an existing definition there must be
    # the one that gets rewritten — otherwise the stale value wins on restart.
    (env_root / ".env.local").write_text("ELEVENLABS_API_KEY=sk_oldoldoldold\n")
    (env_root / ".env").write_text("OTHER=1\n")
    actions.set_elevenlabs_key("sk_newnewnewnew")
    assert "sk_newnewnewnew" in (env_root / ".env.local").read_text()
    assert "ELEVENLABS_API_KEY" not in (env_root / ".env").read_text()


def test_set_rejects_empty_and_whitespace(env_root: Path) -> None:
    with pytest.raises(ValueError):
        actions.set_elevenlabs_key("   ")
    with pytest.raises(ValueError):
        actions.set_elevenlabs_key("sk_bad key")
    assert not (env_root / ".env").exists()
