"""Tests for src/boot/llm.py provider registry — ProviderSpec + _resolve."""

from __future__ import annotations

import pytest

from boot import llm as L


def test_provider_spec_via_proxy_flags():
    # openai and openrouter route through the proxy; Anthropic-wire providers don't.
    assert L.PROVIDERS["openai"].via_proxy is True
    assert L.PROVIDERS["openrouter"].via_proxy is True
    assert L.PROVIDERS["anthropic"].via_proxy is False
    assert L.PROVIDERS["deepseek"].via_proxy is False
    assert L.PROVIDERS["zai"].via_proxy is False


def test_zai_provider_is_direct_anthropic_wire():
    # GLM is reached directly over the Anthropic wire (like DeepSeek): a real
    # upstream base_url, no proxy, default model glm-5.2.
    spec = L.PROVIDERS["zai"]
    assert spec.base_url == "https://api.z.ai/api/anthropic"
    assert spec.api_key_env == "ZAI_API_KEY"
    assert spec.default_model == "glm-5.2"
    assert spec.via_proxy is False
    assert spec.proxy_prefix is None


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
    # A self-referential prefix (provider key or proxy_prefix) is stripped,
    # then re-namespaced for the wire — so a wire-form model id in config.yaml
    # stays stable.
    monkeypatch.setattr(L, "_clients", {})
    _, model, _ = L._resolve("openai", "openai/gpt-5.5")
    assert model == "openai/gpt-5.5"


def test_resolve_direct_provider_stays_bare(monkeypatch):
    monkeypatch.setattr(L, "_clients", {})
    _, model, _ = L._resolve("deepseek", "deepseek-v4-pro")
    assert model == "deepseek-v4-pro"


def test_resolve_zai_uses_default_model_and_base_url(monkeypatch):
    monkeypatch.setattr(L, "_clients", {})
    client, model, extra_body = L._resolve("zai", None)
    assert model == "glm-5.2"  # direct provider — not namespaced
    assert "api.z.ai/api/anthropic" in str(client.base_url)
    assert extra_body == {}


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
    for key in ("anthropic", "deepseek", "zai"):
        assert L.PROVIDERS[key].proxy_prefix is None


def test_openrouter_provider_routes_through_proxy():
    spec = L.PROVIDERS["openrouter"]
    assert spec.via_proxy is True
    assert spec.proxy_prefix == "openrouter"
    assert spec.api_key_env == "OPENROUTER_API_KEY"
    assert spec.base_url == f"http://127.0.0.1:{L.PROXY_PORT}"
    assert spec.proxy_api_base is None  # LiteLLM's default openrouter upstream


def test_resolve_openrouter_slug_passes_through(monkeypatch):
    # OpenRouter model ids are legitimately "vendor/model" — the vendor prefix
    # is part of the id, not informational, and must survive _resolve.
    monkeypatch.setattr(L, "_clients", {})
    _, model, _ = L._resolve("openrouter", "moonshotai/kimi-k2:free")
    assert model == "openrouter/moonshotai/kimi-k2:free"


def test_resolve_openrouter_self_prefix_idempotent(monkeypatch):
    # config.yaml may carry the wire form; stripping the self-prefix then
    # re-namespacing keeps it stable.
    monkeypatch.setattr(L, "_clients", {})
    _, model, _ = L._resolve("openrouter", "openrouter/moonshotai/kimi-k2:free")
    assert model == "openrouter/moonshotai/kimi-k2:free"


def test_resolve_direct_provider_strips_self_prefix(monkeypatch):
    monkeypatch.setattr(L, "_clients", {})
    _, model, _ = L._resolve("anthropic", "anthropic/claude-opus-4-8")
    assert model == "claude-opus-4-8"


def test_resolve_direct_provider_keeps_foreign_prefix(monkeypatch):
    # A non-self prefix is no longer treated as decoration: it's part of the
    # model id and passes through (garbage in, garbage out — the provider 404s).
    monkeypatch.setattr(L, "_clients", {})
    _, model, _ = L._resolve("deepseek", "vendor/some-model")
    assert model == "vendor/some-model"


def test_catalog_rows_reference_known_providers():
    assert len(L.CATALOG) >= 6
    for entry in L.CATALOG:
        assert entry.provider in L.PROVIDERS
        assert entry.model and entry.label
    # The freebies are the OpenRouter draw — at least two must be tagged.
    free = [e for e in L.CATALOG if e.tag == "free"]
    assert len(free) >= 2
    assert all(e.provider == "openrouter" for e in free)
