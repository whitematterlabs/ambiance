"""Tests for src/boot/litellm_proxy.py — fleet detection + config generation."""

from __future__ import annotations

from pathlib import Path

import yaml

from boot import litellm_proxy as lp
from boot import llm as L
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


def _openai_fleet():
    return {"pai": {"provider": "openai", "model": "gpt-5.5"}}


def test_write_config_namespaces_openai_row(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "PAI_ROOT", tmp_path)
    monkeypatch.setattr(lp.C, "load_config", lambda: _openai_fleet())
    path = lp._write_config()
    cfg = yaml.safe_load(path.read_text())
    assert cfg["model_list"] == [
        {
            "model_name": "openai/*",
            "litellm_params": {
                "model": "openai/*",
                "api_key": "os.environ/OPENAI_API_KEY",
            },
        }
    ]
    assert cfg["litellm_settings"]["use_chat_completions_url_for_anthropic_messages"] is True
    assert cfg["litellm_settings"]["request_timeout"] == lp._REQUEST_TIMEOUT
    assert cfg["litellm_settings"]["num_retries"] == lp._NUM_RETRIES


def test_proxied_providers_skips_direct(monkeypatch):
    cfg = {
        "pai": {"provider": "openai", "model": "gpt-5.5"},
        "scribe": {"provider": "deepseek", "model": "deepseek-v4-pro"},
        "root": {"provider": "anthropic", "model": "claude-sonnet-4-6"},
    }
    assert lp._proxied_providers(cfg) == {"openai"}


def test_write_config_emits_api_base_when_set(monkeypatch, tmp_path):
    # Synthesize a second proxied provider with a custom upstream to prove the
    # api_base branch and multi-row emission.
    monkeypatch.setattr(paths, "PAI_ROOT", tmp_path)
    fake = L.ProviderSpec(
        f"http://127.0.0.1:{L.PROXY_PORT}", "GROQ_API_KEY", "llama-4",
        {}, via_proxy=True, proxy_prefix="groq",
        proxy_api_base="https://api.groq.com/openai/v1",
    )
    monkeypatch.setitem(L.PROVIDERS, "groq", fake)
    monkeypatch.setattr(lp.C, "load_config", lambda: {"g": {"provider": "groq", "model": "llama-4"}})
    cfg = yaml.safe_load(lp._write_config().read_text())
    assert cfg["model_list"] == [
        {
            "model_name": "groq/*",
            "litellm_params": {
                "model": "groq/*",
                "api_key": "os.environ/GROQ_API_KEY",
                "api_base": "https://api.groq.com/openai/v1",
            },
        }
    ]
