"""Owner-facing PAI rename: POST /api/rename → config.yaml display_name."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from boot import config as bconfig
from usr.libexec.web.pai_web import actions


@pytest.fixture
def events(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    sent: list[dict] = []
    monkeypatch.setattr(actions, "emit_event", sent.append)
    return sent


def test_rename_writes_config_and_reloads(events, monkeypatch, tmp_path: Path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("pais:\n- name: pai\n  description: dflt\n")
    monkeypatch.setattr(bconfig, "CONFIG_PATH", cfg)
    out = actions.set_pai_display_name("pai", "Muse")
    assert out == {"name": "pai", "display_name": "Muse"}
    data = yaml.safe_load(cfg.read_text())
    assert data["pais"][0]["display_name"] == "Muse"
    assert len(events) == 1
    assert events[0]["kind"] == "kernel:reload_config"
    assert events[0]["action"] == "rename"
    assert events[0]["name"] == "pai"
    assert events[0]["display_name"] == "Muse"


def test_rename_blank_clears_and_reloads(events, monkeypatch, tmp_path: Path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("pais:\n- name: pai\n  display_name: Muse\n")
    monkeypatch.setattr(bconfig, "CONFIG_PATH", cfg)
    out = actions.set_pai_display_name("pai", "   ")
    assert out == {"name": "pai", "display_name": ""}
    assert "display_name" not in yaml.safe_load(cfg.read_text())["pais"][0]
    assert len(events) == 1


def test_rename_unknown_pai_bubbles_and_no_reload(events, monkeypatch, tmp_path: Path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("pais:\n- name: pai\n")
    monkeypatch.setattr(bconfig, "CONFIG_PATH", cfg)
    with pytest.raises(ValueError, match="unknown pai"):
        actions.set_pai_display_name("ghost", "Muse")
    assert events == []
