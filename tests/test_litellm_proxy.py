"""Tests for src/boot/litellm_proxy.py — fleet detection + config generation."""

from __future__ import annotations

from pathlib import Path

import yaml

from boot import litellm_proxy as lp
from boot import paths


def test_fleet_needs_proxy_openai_member():
    cfg = {"writer": {"provider": "openai"}}
    assert lp.fleet_needs_proxy(cfg) is True


def test_fleet_needs_proxy_all_anthropic_deepseek():
    cfg = {
        "root": {"provider": "anthropic"},
        "pai": {},  # no provider -> DEFAULT_PROVIDER (anthropic)
        "cheap": {"provider": "deepseek"},
    }
    assert lp.fleet_needs_proxy(cfg) is False


def test_fleet_needs_proxy_openai_on_dependency():
    # An openai provider that surfaces only on a dependency persub must still
    # be detected (dep override beats the anthropic parent).
    cfg = {
        "root": {
            "provider": "anthropic",
            "dependencies": [{"name": "gpt-helper", "provider": "openai"}],
        }
    }
    assert lp.fleet_needs_proxy(cfg) is True


def test_fleet_needs_proxy_dependency_inherits_anthropic():
    # A dep with no provider inherits its anthropic parent -> no proxy needed.
    cfg = {
        "root": {
            "provider": "anthropic",
            "dependencies": [{"name": "helper"}],
        }
    }
    assert lp.fleet_needs_proxy(cfg) is False


def test_write_config_emits_wildcard_yaml(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(paths, "PAI_ROOT", tmp_path, raising=True)
    out = lp._write_config()
    assert out == tmp_path / "run" / "litellm" / "config.yaml"
    data = yaml.safe_load(out.read_text())
    assert data == {
        "model_list": [
            {"model_name": "*", "litellm_params": {"model": "openai/*"}}
        ]
    }
