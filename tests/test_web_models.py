"""Model-picker backend: catalog + key status + per-PAI provider switching."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from boot import config as bconfig
from boot import llm as L
from usr.libexec.web.pai_web import actions


@pytest.fixture
def env_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(actions.paths, "PAI_ROOT", tmp_path)
    for spec in L.PROVIDERS.values():
        monkeypatch.delenv(spec.api_key_env, raising=False)
    return tmp_path


@pytest.fixture
def events(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    sent: list[dict] = []
    monkeypatch.setattr(actions, "emit_event", sent.append)
    return sent


def test_models_state_rows_mirror_catalog(env_root, monkeypatch):
    monkeypatch.setattr(bconfig, "load_config", lambda: {})
    state = actions.models_state(None)
    assert [(r["provider"], r["model"]) for r in state["rows"]] == [
        (e.provider, e.model) for e in L.CATALOG
    ]
    assert state["current"] is None
    assert all(r["key_status"] == "missing" for r in state["rows"])
    assert set(state["providers"]) == set(L.PROVIDERS)


def test_models_state_key_status_found(env_root, monkeypatch):
    monkeypatch.setattr(bconfig, "load_config", lambda: {})
    (env_root / ".env").write_text("DEEPSEEK_API_KEY=sk-deepseek-test\n")
    state = actions.models_state(None)
    assert state["providers"]["deepseek"]["key_status"] == "found"
    assert state["providers"]["anthropic"]["key_status"] == "missing"
    deepseek_rows = [r for r in state["rows"] if r["provider"] == "deepseek"]
    assert all(r["key_status"] == "found" for r in deepseek_rows)


def test_models_state_current_resolves_defaults(env_root, monkeypatch):
    # A fleet entry with no provider/model pins reports the same defaults
    # reconcile would apply.
    monkeypatch.setattr(bconfig, "load_config", lambda: {"pai": {"pid": 2}})
    state = actions.models_state("pai")
    assert state["current"] == {
        "pai": "pai",
        "provider": L.DEFAULT_PROVIDER,
        "model": L.PROVIDERS[L.DEFAULT_PROVIDER].default_model,
    }


def test_models_state_current_reads_pins(env_root, monkeypatch):
    monkeypatch.setattr(
        bconfig, "load_config",
        lambda: {"pai": {"provider": "zai", "model": "glm-5.2[1m]"}},
    )
    state = actions.models_state("pai")
    assert state["current"] == {"pai": "pai", "provider": "zai", "model": "glm-5.2[1m]"}


def test_models_state_unknown_pai_yields_no_current(env_root, monkeypatch):
    monkeypatch.setattr(bconfig, "load_config", lambda: {})
    assert actions.models_state("ghost")["current"] is None


def test_set_pai_model_writes_config_and_reloads(env_root, events, monkeypatch, tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("pais:\n- name: pai\n  provider: deepseek\n")
    monkeypatch.setattr(bconfig, "CONFIG_PATH", cfg)
    out = actions.set_pai_model("pai", "openrouter", "moonshotai/kimi-k2:free")
    assert out["provider"] == "openrouter"
    data = yaml.safe_load(cfg.read_text())
    assert data["pais"][0]["model"] == "moonshotai/kimi-k2:free"
    assert len(events) == 1
    assert events[0]["kind"] == "kernel:reload_config"
    assert events[0]["action"] == "set-model"
    assert events[0]["name"] == "pai"


def test_set_pai_model_validation_bubbles(env_root, events, monkeypatch, tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("pais:\n- name: pai\n")
    monkeypatch.setattr(bconfig, "CONFIG_PATH", cfg)
    with pytest.raises(ValueError):
        actions.set_pai_model("pai", "grok", "grok-5")
    with pytest.raises(ValueError):
        actions.set_pai_model("ghost", "anthropic", "claude-opus-4-8")
    assert events == []


def test_provider_yaml_plumbing_is_gone():
    for name in ("read_provider", "write_provider", "PROVIDER_CONFIG_PATH", "PROVIDER_OPTIONS"):
        assert not hasattr(actions, name)
