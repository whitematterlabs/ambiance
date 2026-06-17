"""Tests for src/boot/llm.py provider registry — ProviderSpec + _resolve."""

from __future__ import annotations

import pytest

from boot import llm as L


def test_provider_spec_via_proxy_flags():
    # Only openai routes through the proxy; the Anthropic-wire providers don't.
    assert L.PROVIDERS["openai"].via_proxy is True
    assert L.PROVIDERS["anthropic"].via_proxy is False
    assert L.PROVIDERS["deepseek"].via_proxy is False


def test_openai_base_url_built_from_proxy_port():
    # The provider row and the proxy module share one source of truth for the
    # port — the base_url must be derived from PROXY_PORT.
    assert L.PROVIDERS["openai"].base_url == f"http://127.0.0.1:{L.PROXY_PORT}"


def test_resolve_openai_targets_proxy(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    # _resolve caches clients per provider; clear so this test builds fresh.
    monkeypatch.setattr(L, "_clients", {}, raising=True)

    client, model, extra_body = L._resolve("openai", None)
    assert model == "gpt-5.5"  # provider default
    assert f"127.0.0.1:{L.PROXY_PORT}" in str(client.base_url)
    assert extra_body == {}


def test_resolve_unknown_provider_raises():
    with pytest.raises(ValueError, match="unknown provider"):
        L._resolve("not-a-provider", None)


def test_openai_carries_proxy_upstream_descriptor():
    spec = L.PROVIDERS["openai"]
    assert spec.via_proxy is True
    assert spec.proxy_prefix == "openai"
    assert spec.proxy_api_base is None  # defaults to api.openai.com


def test_direct_providers_have_no_proxy_prefix():
    for key in ("anthropic", "deepseek"):
        assert L.PROVIDERS[key].proxy_prefix is None
