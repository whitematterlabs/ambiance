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
    monkeypatch.setattr(L, "_clients", {})
    client, model, extra_body = L._resolve("openai", None)
    assert model == "openai/gpt-5.5"  # provider-namespaced for proxy routing
    assert f"127.0.0.1:{L.PROXY_PORT}" in str(client.base_url)
    assert extra_body == {}


def test_resolve_proxied_namespaces_explicit_model(monkeypatch):
    monkeypatch.setattr(L, "_clients", {})
    _, model, _ = L._resolve("openai", "gpt-5.5-mini")
    assert model == "openai/gpt-5.5-mini"


def test_resolve_proxied_normalizes_incoming_prefix(monkeypatch):
    # An incoming OpenRouter-style prefix is normalized to bare, then
    # re-namespaced with the provider's proxy_prefix.
    monkeypatch.setattr(L, "_clients", {})
    _, model, _ = L._resolve("openai", "anthropic/gpt-5.5")
    assert model == "openai/gpt-5.5"


def test_resolve_direct_provider_stays_bare(monkeypatch):
    monkeypatch.setattr(L, "_clients", {})
    _, model, _ = L._resolve("deepseek", "deepseek-v4-pro")
    assert model == "deepseek-v4-pro"


def test_resolve_sets_client_timeout(monkeypatch):
    monkeypatch.setattr(L, "_clients", {})
    client, _, _ = L._resolve("anthropic", None)
    assert client.timeout.read == L._CLIENT_TIMEOUT.read


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
