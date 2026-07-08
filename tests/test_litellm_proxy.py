"""Tests for src/boot/litellm_proxy.py — fleet detection, config generation,
and reconcile's restart-on-drift behavior."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
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


def test_write_config_emits_openrouter_row(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "PAI_ROOT", tmp_path)
    monkeypatch.setattr(lp.C, "load_config", lambda: {"pai": {"provider": "openrouter", "model": "moonshotai/kimi-k2:free"}})
    cfg_path = lp._write_config()
    cfg = yaml.safe_load(cfg_path.read_text())
    rows = {r["model_name"]: r["litellm_params"] for r in cfg["model_list"]}
    assert "openrouter/*" in rows
    assert rows["openrouter/*"]["api_key"] == "os.environ/OPENROUTER_API_KEY"


def test_fleet_needs_proxy_openrouter_member(monkeypatch):
    monkeypatch.setattr(
        lp.C, "load_config",
        lambda: {"pai": {"provider": "openrouter", "model": "moonshotai/kimi-k2:free"}},
    )
    assert lp.fleet_needs_proxy() is True


# ── reconcile: restart a running-but-stale proxy ────────────────────────────
#
# The spawn/stop internals are stubbed (same idiom as the config tests above:
# monkeypatch lp's module attributes); reconcile's job here is only the
# *decision* — regenerate the config, compare, restart or not.


@pytest.fixture
def running_proxy(monkeypatch, tmp_path):
    """Proxy tracked as running; returns the call log for _stop/_spawn."""
    monkeypatch.setattr(paths, "PAI_ROOT", tmp_path)
    monkeypatch.setattr(lp.supervisor, "is_tracked", lambda slug: True)
    calls: list[str] = []

    async def fake_stop():
        calls.append("stop")

    async def fake_spawn():
        calls.append("spawn")

    monkeypatch.setattr(lp, "_stop", fake_stop)
    monkeypatch.setattr(lp, "_spawn", fake_spawn)
    return calls


def test_reconcile_restarts_on_fleet_provider_change(running_proxy, monkeypatch):
    # Proxy was spawned for an openai-only fleet; its on-disk config says so.
    monkeypatch.setattr(lp.C, "load_config", lambda: _openai_fleet())
    lp._write_config()
    # Owner switches a PAI to openrouter → regenerated config gains a row →
    # the running proxy (which lacks it) must be restarted.
    monkeypatch.setattr(
        lp.C, "load_config",
        lambda: {
            "pai": {"provider": "openai", "model": "gpt-5.5"},
            "scribe": {"provider": "openrouter", "model": "moonshotai/kimi-k2:free"},
        },
    )
    asyncio.run(lp.reconcile())
    assert running_proxy == ["stop", "spawn"]
    cfg = yaml.safe_load(lp._config_path().read_text())
    names = {r["model_name"] for r in cfg["model_list"]}
    assert names == {"openai/*", "openrouter/*"}


def test_reconcile_restarts_on_proxied_key_entry(running_proxy, monkeypatch):
    # Config bytes unchanged, but the reload was a set-api-key for a proxied
    # provider: the key lives in the proxy's env snapshot, so restart anyway.
    monkeypatch.setattr(lp.C, "load_config", lambda: _openai_fleet())
    lp._write_config()
    event = {"kind": "kernel:reload_config", "action": "set-api-key", "provider": "openai"}
    asyncio.run(lp.reconcile(event))
    assert running_proxy == ["stop", "spawn"]


def test_reconcile_ignores_direct_provider_key_entry(running_proxy, monkeypatch):
    # A key for a provider the kernel talks to directly (anthropic) never
    # touches the proxy — no restart.
    monkeypatch.setattr(
        lp.C, "load_config",
        lambda: {
            "pai": {"provider": "openai", "model": "gpt-5.5"},
            "root": {"provider": "anthropic", "model": "claude-sonnet-4-6"},
        },
    )
    lp._write_config()
    event = {"kind": "kernel:reload_config", "action": "set-api-key", "provider": "anthropic"}
    asyncio.run(lp.reconcile(event))
    assert running_proxy == []


def test_reconcile_noop_when_running_and_unchanged(running_proxy, monkeypatch):
    monkeypatch.setattr(lp.C, "load_config", lambda: _openai_fleet())
    lp._write_config()
    asyncio.run(lp.reconcile())
    assert running_proxy == []


def test_reconcile_spawns_when_newly_needed(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "PAI_ROOT", tmp_path)
    monkeypatch.setattr(lp.C, "load_config", lambda: _openai_fleet())
    monkeypatch.setattr(lp.supervisor, "is_tracked", lambda slug: False)
    calls: list[str] = []

    async def fake_spawn():
        calls.append("spawn")

    monkeypatch.setattr(lp, "_spawn", fake_spawn)
    asyncio.run(lp.reconcile())
    assert calls == ["spawn"]


def test_reconcile_stops_when_no_longer_needed(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "PAI_ROOT", tmp_path)
    monkeypatch.setattr(lp.C, "load_config", lambda: {"root": {"provider": "anthropic"}})
    monkeypatch.setattr(lp.supervisor, "is_tracked", lambda slug: True)
    calls: list[str] = []

    async def fake_stop():
        calls.append("stop")

    monkeypatch.setattr(lp, "_stop", fake_stop)
    asyncio.run(lp.reconcile())
    assert calls == ["stop"]
