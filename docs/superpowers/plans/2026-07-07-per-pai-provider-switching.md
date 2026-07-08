# Per-PAI Provider Switching Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Switch any PAI's provider+model from a web-console dialog (with API-key entry for missing keys), and add OpenRouter as a provider.

**Architecture:** The kernel already routes per-PAI `provider:`/`model:` from `/etc/config.yaml` through `/proc/<pai>/spec.yaml` to `llm._resolve` every turn. This plan adds: an `openrouter` ProviderSpec + curated `CATALOG` in `boot/llm.py`; a `.env` hot-reload on `kernel:reload_config`; a `set_pai_model` config mutation in `boot/config.py`; three web endpoints (`GET/POST /api/models`, `POST /api/apikey`); and a `ModelPicker` dialog replacing the vestigial CommandPalette. The dead `provider.yaml` plumbing is deleted.

**Tech Stack:** Python 3.14 (uv), http.server backend (`src/usr/libexec/web/pai_web/`), React+Vite frontend (`src/usr/libexec/web/src/`), pytest.

**Spec:** `docs/superpowers/specs/2026-07-07-per-pai-provider-switching-design.md`

## Global Constraints

- **Execute in a fresh git worktree from `origin/main`** (superpowers:using-git-worktrees). The main checkout has *unrelated in-flight changes* (cowork capability granularity, touching `actions.py`, `config.py`, `Header.tsx`, `capture.ts`, `test_config.py`) that must not be swept into commits. All plan work happens in different regions of those files; merge to main at the end.
- Tests: `uv run python -m pytest tests/<file> -v` (run `uv sync` once in the worktree first).
- Frontend build check: `cd src/usr/libexec/web && npm install && npm run build` (tsc + vite; there are no frontend unit tests — the build is the gate).
- Keys never round-trip to the browser: API responses report only `found`/`missing`.
- All code lives in this repo (kernel + web console). No pairegistry sync needed — no dual-homed bins are touched.
- Commit messages end with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

---

### Task 1: `boot/llm.py` — openrouter provider, prefix-strip fix, CATALOG

**Files:**
- Modify: `src/boot/llm.py` (PROVIDERS dict ~line 129, `_resolve` ~line 189, new CatalogEntry/CATALOG after DEFAULT_PROVIDER ~line 165)
- Test: `tests/test_llm_provider.py`, `tests/test_litellm_proxy.py`

**Interfaces:**
- Produces: `PROVIDERS["openrouter"]` (ProviderSpec, `via_proxy=True`, `proxy_prefix="openrouter"`, `api_key_env="OPENROUTER_API_KEY"`); `CatalogEntry` dataclass `(provider: str, model: str, label: str, tag: Optional[str] = None)`; `CATALOG: tuple[CatalogEntry, ...]`. Tasks 4–5 import both.
- New `_resolve` prefix rule: strips a leading `<key>/` or `<proxy_prefix>/` only when self-referential; other `vendor/model` slugs pass through intact.

- [ ] **Step 1: Verify current OpenRouter free-model slugs** (no key needed):

```bash
curl -s https://openrouter.ai/api/v1/models | python3 -c "import json,sys; [print(m['id']) for m in json.load(sys.stdin)['data'] if m['id'].endswith(':free')]" | head -20
```

Pick 3 well-known free slugs (prefer a Kimi, a DeepSeek, and a Qwen coder variant). If the fetch fails (offline), use the fallbacks written in Step 4 verbatim.

- [ ] **Step 2: Write the failing tests** — append to `tests/test_llm_provider.py`:

```python
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
```

Update two existing tests in the same file:

```python
def test_provider_spec_via_proxy_flags():
    # openai and openrouter route through the proxy; Anthropic-wire providers don't.
    assert L.PROVIDERS["openai"].via_proxy is True
    assert L.PROVIDERS["openrouter"].via_proxy is True
    assert L.PROVIDERS["anthropic"].via_proxy is False
    assert L.PROVIDERS["deepseek"].via_proxy is False
    assert L.PROVIDERS["zai"].via_proxy is False


def test_resolve_proxied_normalizes_incoming_prefix(monkeypatch):
    # A self-referential prefix (provider key or proxy_prefix) is stripped,
    # then re-namespaced for the wire — so a wire-form model id in config.yaml
    # stays stable.
    monkeypatch.setattr(L, "_clients", {})
    _, model, _ = L._resolve("openai", "openai/gpt-5.5")
    assert model == "openai/gpt-5.5"
```

Append to `tests/test_litellm_proxy.py` (mirror `test_write_config_namespaces_openai_row` at line 55 — read it first and reuse its monkeypatch shape exactly):

```python
def test_write_config_emits_openrouter_row(monkeypatch, tmp_path):
    monkeypatch.setattr(
        lp.C, "load_config",
        lambda: {"pai": {"provider": "openrouter", "model": "moonshotai/kimi-k2:free"}},
    )
    monkeypatch.setattr(lp.paths, "run", lambda: tmp_path)
    cfg_path = lp._write_config()
    import yaml
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
```

(Adjust the two monkeypatch lines to match how the existing tests in that file stub `load_config`/`paths.run` — copy their exact idiom.)

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run python -m pytest tests/test_llm_provider.py tests/test_litellm_proxy.py -v`
Expected: new tests FAIL with `KeyError: 'openrouter'` / `AttributeError: ... CATALOG`; updated `test_provider_spec_via_proxy_flags` FAILS.

- [ ] **Step 4: Implement.** In `src/boot/llm.py`:

Add to `PROVIDERS` (after the `openai` entry):

```python
    # OpenRouter is OpenAI-wire upstream, so it also routes through the
    # LiteLLM proxy. Its model ids are "vendor/model" slugs (the vendor prefix
    # is part of the id — see _resolve's self-prefix rule). The default is a
    # free model on purpose: it's the zero-cost way to try PAI.
    "openrouter": ProviderSpec(
        f"http://127.0.0.1:{PROXY_PORT}",
        "OPENROUTER_API_KEY",
        "moonshotai/kimi-k2:free",
        {},
        via_proxy=True,
        proxy_prefix="openrouter",
    ),
```

(Substitute the default model and the CATALOG free rows below with the slugs verified in Step 1.)

After `DEFAULT_PROVIDER = "anthropic"` add:

```python
@dataclass(frozen=True)
class CatalogEntry:
    """One curated row in the web console's model picker."""

    provider: str
    model: str
    label: str
    tag: Optional[str] = None


# Curated provider+model combos the console offers one-click. Single source of
# truth — the web backend (pai_web/actions.models_state) imports this. Any
# model id outside the catalog still works via the picker's custom-model row.
CATALOG: tuple[CatalogEntry, ...] = (
    CatalogEntry("deepseek", "deepseek-v4-pro", "DeepSeek V4-Pro"),
    CatalogEntry("anthropic", "claude-opus-4-8", "Claude Opus 4.8"),
    CatalogEntry("anthropic", "claude-sonnet-4-6", "Claude Sonnet 4-6"),
    CatalogEntry("openai", "gpt-5.5", "ChatGPT-5.5"),
    CatalogEntry("zai", "glm-5.2", "GLM 5.2"),
    CatalogEntry("openrouter", "moonshotai/kimi-k2:free", "Kimi K2 (free)", "free"),
    CatalogEntry("openrouter", "deepseek/deepseek-r1:free", "DeepSeek R1 (free)", "free"),
    CatalogEntry("openrouter", "qwen/qwen3-coder:free", "Qwen3 Coder (free)", "free"),
)
```

In `_resolve`, replace:

```python
    # Normalize any incoming OpenRouter-style prefix to a bare model id.
    if model and "/" in model:
        model = model.split("/", 1)[1]
```

with:

```python
    # Strip only a self-referential prefix (e.g. "anthropic/claude-…" sent to
    # the anthropic provider, or the wire form "openai/gpt-5.5" round-tripping
    # from config). Any other slash is part of the model id — OpenRouter slugs
    # are "vendor/model" and must reach the proxy intact.
    if model and "/" in model:
        head, rest = model.split("/", 1)
        if head == key or head == spec.proxy_prefix:
            model = rest
```

Also update the `_resolve` docstring paragraph about OpenRouter-style prefixes to match the new rule.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run python -m pytest tests/test_llm_provider.py tests/test_litellm_proxy.py -v`
Expected: ALL PASS.

- [ ] **Step 6: Commit**

```bash
git add src/boot/llm.py tests/test_llm_provider.py tests/test_litellm_proxy.py
git commit -m "kernel: openrouter provider + model catalog; _resolve keeps vendor/model slugs"
```

---

### Task 2: `.env` hot-reload on `kernel:reload_config`

**Files:**
- Modify: `src/boot/__init__.py` (append function), `src/boot/main.py` (`_handle_reload_config`, ~line 899 inside the lock-holding `try:`)
- Test: `tests/test_reload_env.py` (create)

**Interfaces:**
- Consumes: module globals `_pai_root`, `_code_root`, `_load_dotenv` already defined in `boot/__init__.py`.
- Produces: `boot.reload_env() -> None`. The web backend never calls it (web writes `.env` + emits the event); only the kernel handler does.

- [ ] **Step 1: Write the failing tests** — create `tests/test_reload_env.py`:

```python
"""boot.reload_env — runtime re-read of .env so web-entered keys go live."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import boot


@pytest.fixture
def roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    pai_root = tmp_path / "pai_root"
    code_root = tmp_path / "code_root"
    pai_root.mkdir()
    code_root.mkdir()
    monkeypatch.setattr(boot, "_pai_root", pai_root)
    monkeypatch.setattr(boot, "_code_root", code_root)
    monkeypatch.delenv("PAI_TEST_RELOAD_KEY", raising=False)
    return pai_root, code_root


def test_reload_overrides_stale_process_env(roots, monkeypatch):
    # The whole point: a key replaced in .env must beat the value the process
    # loaded at boot (override=False left it stale in os.environ).
    pai_root, _ = roots
    (pai_root / ".env").write_text("PAI_TEST_RELOAD_KEY=fresh\n")
    monkeypatch.setenv("PAI_TEST_RELOAD_KEY", "stale")
    boot.reload_env()
    assert os.environ["PAI_TEST_RELOAD_KEY"] == "fresh"


def test_reload_precedence_matches_boot(roots):
    # Boot precedence: pai_root beats code_root; .env.local beats .env.
    pai_root, code_root = roots
    (code_root / ".env").write_text("PAI_TEST_RELOAD_KEY=code_env\n")
    (code_root / ".env.local").write_text("PAI_TEST_RELOAD_KEY=code_local\n")
    (pai_root / ".env").write_text("PAI_TEST_RELOAD_KEY=pai_env\n")
    boot.reload_env()
    assert os.environ["PAI_TEST_RELOAD_KEY"] == "pai_env"
    (pai_root / ".env.local").write_text("PAI_TEST_RELOAD_KEY=pai_local\n")
    boot.reload_env()
    assert os.environ["PAI_TEST_RELOAD_KEY"] == "pai_local"


def test_reload_missing_files_is_noop(roots, monkeypatch):
    monkeypatch.setenv("PAI_TEST_RELOAD_KEY", "kept")
    boot.reload_env()
    assert os.environ["PAI_TEST_RELOAD_KEY"] == "kept"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/test_reload_env.py -v`
Expected: FAIL with `AttributeError: module 'boot' has no attribute 'reload_env'`.

- [ ] **Step 3: Implement.** Append to `src/boot/__init__.py`:

```python
def reload_env() -> None:
    """Re-read the .env files so runtime edits (web-console key entry) go live.

    Boot loads with override=False, high-precedence file first. To keep the
    same precedence with override=True we load lowest-precedence first so the
    later files win. override=True means .env values now beat inherited shell
    exports — intended: the console's key editor writes .env and must win over
    a stale exported var. Called by the kernel on kernel:reload_config.
    """
    for _base in (_code_root, _pai_root):
        for _name in (".env", ".env.local"):
            _load_dotenv(_base / _name, override=True)
```

In `src/boot/main.py`, in `_handle_reload_config`, immediately before `C.reconcile_from_config()` (inside the same `try:`), add:

```python
            # Keys entered from the web console land in $PAI_ROOT/.env after
            # boot already snapshotted the env; re-read them and rebuild the
            # per-provider clients (they capture the key at construction).
            import boot as _boot
            from . import llm as _llm
            _boot.reload_env()
            _llm._clients.clear()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/test_reload_env.py tests/test_boot_smoke.py -v`
Expected: ALL PASS (smoke test guards the main.py import).

- [ ] **Step 5: Commit**

```bash
git add src/boot/__init__.py src/boot/main.py tests/test_reload_env.py
git commit -m "kernel: hot-reload .env + rebuild llm clients on reload_config"
```

---

### Task 3: `boot/config.py` — `set_pai_model` mutation

**Files:**
- Modify: `src/boot/config.py` (add function directly after `set_capability_mode`, ~line 580)
- Test: `tests/test_config.py` (append)

**Interfaces:**
- Consumes: `CONFIG_PATH`, `_load_yaml`, `L.PROVIDERS` (all already in `config.py` — it imports `llm as L`).
- Produces: `set_pai_model(name: str, provider: str, model: str, path: Path | None = None) -> dict[str, str]` returning `{"name", "provider", "model"}`. Task 5's web action calls it.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_config.py` (import `set_pai_model` alongside the module's existing config import idiom — read the top of the file and match it):

```python
def test_set_pai_model_rewrites_only_target(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "capabilities:\n"
        "  email_send: ask\n"
        "pais:\n"
        "- name: root\n"
        "  provider: deepseek\n"
        "  model: deepseek-v4-pro\n"
        "- name: pai\n"
        "  provider: deepseek\n"
        "  model: deepseek-v4-pro\n"
        "  fallback: true\n"
    )
    out = config.set_pai_model("pai", "openrouter", "moonshotai/kimi-k2:free", path=cfg)
    assert out == {"name": "pai", "provider": "openrouter", "model": "moonshotai/kimi-k2:free"}
    data = yaml.safe_load(cfg.read_text())
    by_name = {e["name"]: e for e in data["pais"]}
    assert by_name["pai"]["provider"] == "openrouter"
    assert by_name["pai"]["model"] == "moonshotai/kimi-k2:free"
    assert by_name["pai"]["fallback"] is True          # untouched siblings keys
    assert by_name["root"]["provider"] == "deepseek"   # untouched sibling entry
    assert data["capabilities"] == {"email_send": "ask"}  # untouched other sections


def test_set_pai_model_unknown_provider(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("pais:\n- name: pai\n")
    with pytest.raises(ValueError, match="unknown provider"):
        config.set_pai_model("pai", "grok", "grok-5", path=cfg)


def test_set_pai_model_unknown_pai(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("pais:\n- name: pai\n")
    with pytest.raises(ValueError, match="unknown pai"):
        config.set_pai_model("ghost", "anthropic", "claude-opus-4-8", path=cfg)


def test_set_pai_model_rejects_empty_model(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("pais:\n- name: pai\n")
    with pytest.raises(ValueError, match="model"):
        config.set_pai_model("pai", "anthropic", "   ", path=cfg)


def test_set_pai_model_malformed_yaml_leaves_file_untouched(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("pais: [unclosed\n")
    before = cfg.read_text()
    with pytest.raises(Exception):
        config.set_pai_model("pai", "anthropic", "claude-opus-4-8", path=cfg)
    assert cfg.read_text() == before
```

(If `tests/test_config.py` imports the module under a different name than `config`, or lacks `yaml`/`pytest` imports at top, follow its existing convention.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/test_config.py -k set_pai_model -v`
Expected: FAIL with `AttributeError: ... has no attribute 'set_pai_model'`.

- [ ] **Step 3: Implement.** Add to `src/boot/config.py` after `set_capability_mode`:

```python
def set_pai_model(name: str, provider: str, model: str, path: Path | None = None) -> dict[str, str]:
    """Write `provider:`/`model:` on one fleet entry and return them.

    Strict like set_capability_mode: an unknown provider or absent PAI raises
    ValueError (the web surface maps it to a 400). Full-document round-trip via
    the same yaml path paiadd uses — comments don't survive, which is the trade
    the fleet block already lives with. Atomic (tmp + rename); on any failure
    the file is untouched. The caller emits `kernel:reload_config`.
    """
    if provider not in L.PROVIDERS:
        known = ", ".join(sorted(L.PROVIDERS))
        raise ValueError(f"unknown provider {provider!r} (known: {known})")
    model = model.strip()
    if not model:
        raise ValueError("model must be non-empty")
    p = path or CONFIG_PATH
    data = _load_yaml(p) if p.exists() else {}
    pais = data.get("pais") if isinstance(data, dict) else None
    entry = None
    if isinstance(pais, list):
        entry = next(
            (e for e in pais if isinstance(e, dict) and e.get("name") == name), None
        )
    if entry is None:
        raise ValueError(f"unknown pai: {name!r}")
    entry["provider"] = provider
    entry["model"] = model
    tmp = p.with_suffix(p.suffix + ".tmp")
    with tmp.open("w") as f:
        yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True)
    tmp.rename(p)
    return {"name": name, "provider": provider, "model": model}
```

(If `_load_yaml` raises something other than a `ValueError` subclass on malformed yaml, that's fine — the malformed test catches `Exception` and the web layer 500s it per spec.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/test_config.py -v`
Expected: ALL PASS (including the pre-existing config tests).

- [ ] **Step 5: Commit**

```bash
git add src/boot/config.py tests/test_config.py
git commit -m "config: set_pai_model — per-PAI provider/model mutation, atomic + strict"
```

---

### Task 4: web backend — generalized env-key helpers + `set_api_key`

**Files:**
- Modify: `src/usr/libexec/web/pai_web/actions.py` (ElevenLabs key section, ~lines 316–376)
- Test: `tests/test_web_api_key.py` (create); `tests/test_web_elevenlabs_key.py` must stay green unmodified.

**Interfaces:**
- Consumes: `L.PROVIDERS` from Task 1 (`from boot import llm` — add the import at top of `actions.py` next to the other `boot` imports).
- Produces: `_dotenv_lookup(var: str) -> str | None`; `_write_env_var(var: str, value: str) -> None`; `set_api_key(provider: str, key: str) -> dict` returning `{"provider", "key_status": "found"}`. Task 5 uses `_dotenv_lookup`; the server route uses `set_api_key`.

- [ ] **Step 1: Write the failing tests** — create `tests/test_web_api_key.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/test_web_api_key.py -v`
Expected: FAIL with `AttributeError: ... 'set_api_key'`.

- [ ] **Step 3: Implement.** In `actions.py`, rework the ElevenLabs key section into general helpers (keep the section, retitle its banner comment to `── API keys (web-managed) ──…`):

```python
def _dotenv_lookup(var: str) -> str | None:
    """Resolve an env var the way boot does: process env, then .env.local/.env."""
    from dotenv import dotenv_values

    val = os.environ.get(var)
    if val:
        return val
    for path in _env_files():
        try:
            val = dotenv_values(path).get(var)
        except OSError:
            val = None
        if val:
            return val
    return None


def _write_env_var(var: str, value: str) -> None:
    """Persist VAR=value where boot will re-read it, live for this process too.

    Writes to whichever env file already defines the var (so .env.local keeps
    shadowing .env across restarts), else to $PAI_ROOT/.env, chmod 600 — the
    file the installer seeds keys into.
    """
    from dotenv import dotenv_values, set_key

    value = value.strip()
    if not value:
        raise ValueError("API key is empty")
    if any(c.isspace() for c in value):
        raise ValueError("API key must not contain whitespace")

    target = None
    for path in _env_files():
        try:
            if path.is_file() and dotenv_values(path).get(var):
                target = path
                break
        except OSError:
            continue
    if target is None:
        target = paths.PAI_ROOT / ".env"
        target.touch(exist_ok=True)
    set_key(target, var, value)
    try:
        target.chmod(0o600)
    except OSError:
        pass
    os.environ[var] = value


def set_api_key(provider: str, key: str) -> dict:
    """Store an LLM provider key from the console and make the kernel re-read it.

    The key rides only the .env file — never the reload event, never a
    response body. `key_status` is all the browser ever learns.
    """
    spec = llm.PROVIDERS.get(provider)
    if spec is None:
        raise ValueError(f"unknown provider: {provider!r}")
    _write_env_var(spec.api_key_env, key)
    emit_event({
        "kind": "kernel:reload_config",
        "source": "web",
        "action": "set-api-key",
        "provider": provider,
    })
    return {"provider": provider, "key_status": "found"}
```

Add `from boot import llm` next to the other `boot` imports at the top. Rewrite the two existing ElevenLabs functions onto the helpers, preserving exact behavior (their tests stay green):

```python
def elevenlabs_key_status() -> dict:
    """Whether an ElevenLabs key is configured, plus a masked hint.

    Mirrors the resolution order the TTS provider sees: process env first
    (voice_cloud reloads dotenv with override=False), then .env.local / .env.
    """
    key = _dotenv_lookup(_ELEVENLABS_ENV_VAR)
    hint = f"…{key[-4:]}" if key and len(key) >= 8 else None
    return {"set": bool(key), "hint": hint}


def set_elevenlabs_key(key: str) -> dict:
    """Persist the ElevenLabs API key and make it live for this process."""
    _write_env_var(_ELEVENLABS_ENV_VAR, key)
    return elevenlabs_key_status()
```

- [ ] **Step 4: Run tests to verify they pass — including the untouched ElevenLabs suite**

Run: `uv run python -m pytest tests/test_web_api_key.py tests/test_web_elevenlabs_key.py -v`
Expected: ALL PASS. (Note: `test_set_rejects_empty_and_whitespace` asserts `.env` is *not created* on rejection — `_write_env_var` must validate before `touch`, as written above.)

- [ ] **Step 5: Commit**

```bash
git add src/usr/libexec/web/pai_web/actions.py tests/test_web_api_key.py
git commit -m "web: generalize env-key helpers; set_api_key for any LLM provider"
```

---

### Task 5: web backend — models endpoints + delete provider.yaml plumbing

**Files:**
- Modify: `src/usr/libexec/web/pai_web/actions.py` (delete lines ~39–41 `PROVIDER_CONFIG_PATH`/`PROVIDER_OPTIONS`/`_VALID_PROVIDERS` and ~74–88 `read_provider`/`write_provider`; add `models_state`/`set_pai_model`)
- Modify: `src/usr/libexec/web/pai_web/server.py` (GET ~206, POST ~278–281, SSE hello ~407)
- Modify: `src/usr/libexec/web/pai_web/hub.py` (`snapshot`, ~line 347)
- Modify: `src/usr/libexec/web/README.md` (route table), `src/usr/libexec/web/CAPABILITIES.md` (provider bullet)
- Test: `tests/test_web_models.py` (create)

**Interfaces:**
- Consumes: `L.CATALOG`, `L.PROVIDERS`, `L.DEFAULT_PROVIDER` (Task 1); `C.set_pai_model` (Task 3); `_dotenv_lookup` (Task 4).
- Produces: `actions.models_state(pai: str | None) -> dict` shaped `{"rows": [{provider, model, label, tag, key_status}], "providers": {key: {key_status, api_key_env, default_model}}, "current": {pai, provider, model} | None}`; `actions.set_pai_model(name, provider, model) -> dict`; routes `GET /api/models?pai=<slug>`, `POST /api/models {pai, provider, model}`, `POST /api/apikey {provider, key}`; `HUB.snapshot()` now takes no argument. Task 6's frontend consumes these exact shapes.

- [ ] **Step 1: Write the failing tests** — create `tests/test_web_models.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/test_web_models.py -v`
Expected: FAIL (`models_state` missing; plumbing-gone test fails while the old symbols exist).

- [ ] **Step 3: Implement `actions.py`.** Delete `PROVIDER_CONFIG_PATH`, `PROVIDER_OPTIONS`, `_VALID_PROVIDERS`, `read_provider`, `write_provider` (and the now-unused `from sbin.tui.state import HOME_DIR` *only if* nothing else in the file uses `HOME_DIR`/`today_file` — check first). Add `import threading` to the imports if not present. Add near the key section:

```python
# Serializes config.yaml read-modify-write against concurrent POSTs — the web
# server is threaded and two rapid switches would otherwise lose an update.
_config_write_lock = threading.Lock()


def models_state(pai: str | None) -> dict:
    """Catalog rows + per-provider key status + one PAI's current selection.

    `current` reads /etc/config.yaml — the file POST /api/models edits — so a
    re-fetch right after a switch is never stale, resolving absent
    provider/model to the same defaults reconcile applies.
    """
    from boot import config as bconfig

    providers = {
        key: {
            "key_status": "found" if _dotenv_lookup(spec.api_key_env) else "missing",
            "api_key_env": spec.api_key_env,
            "default_model": spec.default_model,
        }
        for key, spec in llm.PROVIDERS.items()
    }
    rows = [
        {
            "provider": e.provider,
            "model": e.model,
            "label": e.label,
            "tag": e.tag,
            "key_status": providers[e.provider]["key_status"],
        }
        for e in llm.CATALOG
    ]
    current = None
    if pai:
        entry = bconfig.load_config().get(pai)
        if entry is not None:
            provider = entry.get("provider") or llm.DEFAULT_PROVIDER
            model = entry.get("model") or llm.PROVIDERS[provider].default_model
            current = {"pai": pai, "provider": provider, "model": model}
    return {"rows": rows, "providers": providers, "current": current}


def set_pai_model(name: str, provider: str, model: str) -> dict:
    """Switch one PAI's provider/model in config.yaml and reload the kernel."""
    from boot import config as bconfig

    with _config_write_lock:
        out = bconfig.set_pai_model(name, provider, model)
    emit_event({
        "kind": "kernel:reload_config",
        "source": "web",
        "action": "set-model",
        "name": name,
        "provider": provider,
        "model": model,
    })
    return out
```

- [ ] **Step 4: Implement `server.py` + `hub.py`.** In `server.py` `do_GET`, replace the `/api/state` line and add `/api/models`:

```python
        if path == "/api/state":
            return self._json(HUB.snapshot())
        if path == "/api/models":
            query = self.path.split("?", 1)[1] if "?" in self.path else ""
            vals = urllib.parse.parse_qs(query).get("pai")
            return self._json({"ok": True, **actions.models_state(vals[0] if vals else None)})
```

In `do_POST`, delete the `/api/provider` block and add (next to the other routes):

```python
            if path == "/api/models":
                result = actions.set_pai_model(
                    str(body["pai"]), str(body["provider"]), str(body["model"])
                )
                return self._json({"ok": True, **result})
            if path == "/api/apikey":
                result = actions.set_api_key(str(body["provider"]), str(body["key"]))
                return self._json({"ok": True, **result})
```

At ~line 407 replace `self._sse_send(HUB.snapshot(actions.read_provider()))` with `self._sse_send(HUB.snapshot())`.

In `hub.py`, change `def snapshot(self, provider: str) -> dict:` to `def snapshot(self) -> dict:` and delete the `"provider": provider,` line. Update the docstring/comment at ~line 301 (`Subscribers receive: … provider`) to drop `provider`.

- [ ] **Step 5: Update docs.** In `src/usr/libexec/web/README.md`, replace the `/api/provider` row of the route table with:

```markdown
| GET  | `/api/models` | catalog + key status (+ `?pai=` current selection) |
| POST | `/api/models` | `{pai, provider, model}` → rewrite that PAI's config.yaml entry + reload |
| POST | `/api/apikey` | `{provider, key}` → store key in `$PAI_ROOT/.env` + reload |
```

In `src/usr/libexec/web/CAPABILITIES.md`, replace the provider bullet (the one citing `ProviderCommands`, `set_provider`) with a line describing per-PAI switching via the model picker, sourced from `models_state`/`set_pai_model`.

- [ ] **Step 6: Run the web test suite**

Run: `uv run python -m pytest tests/test_web_models.py tests/ -k "web" -v`
Expected: ALL PASS (auth, kernel, approvals, elevenlabs, etc. unaffected; anything importing `read_provider` would surface here).

Also grep for stragglers: `rg -n "read_provider|write_provider|api/provider|PROVIDER_OPTIONS" src/ tests/` — expect zero hits outside this plan's own docs.

- [ ] **Step 7: Commit**

```bash
git add src/usr/libexec/web/pai_web/actions.py src/usr/libexec/web/pai_web/server.py \
        src/usr/libexec/web/pai_web/hub.py src/usr/libexec/web/README.md \
        src/usr/libexec/web/CAPABILITIES.md tests/test_web_models.py
git commit -m "web: /api/models + /api/apikey; delete vestigial provider.yaml plumbing"
```

---

### Task 6: frontend — ModelPicker dialog, header button, palette retirement

**Files:**
- Create: `src/usr/libexec/web/src/components/ModelPicker.tsx`
- Delete: `src/usr/libexec/web/src/components/CommandPalette.tsx`
- Modify: `src/usr/libexec/web/src/api.ts`, `src/usr/libexec/web/src/types.ts`, `src/usr/libexec/web/src/App.tsx`, `src/usr/libexec/web/src/styles.css`

**Interfaces:**
- Consumes: the Task 5 wire shapes, verbatim.
- Produces: `<ModelPicker pai={slug} onClose={} onStatus={} onSwitched={} />`; App-level `models: ModelsState | null` state driving the header **Model** button label.

- [ ] **Step 1: types.ts.** Remove the `provider` field from the `hello` message interface and the `{ type: "provider"; provider: string }` variant from `ServerMessage` (find them with `rg -n "provider" src/types.ts`). Add:

```ts
export interface ModelRow {
  provider: string;
  model: string;
  label: string;
  tag: string | null;
  key_status: "found" | "missing";
}

export interface ModelsState {
  rows: ModelRow[];
  providers: Record<
    string,
    { key_status: "found" | "missing"; api_key_env: string; default_model: string }
  >;
  current: { pai: string; provider: string; model: string } | null;
}
```

- [ ] **Step 2: api.ts.** Delete `export const setProvider = …`. Add:

```ts
import type { ModelsState } from "./types";

// Model picker: catalog + key status (GET), per-PAI switch (POST), key entry.
export const getModels = (pai: string | null) =>
  get(`/api/models${pai ? `?pai=${encodeURIComponent(pai)}` : ""}`) as Promise<
    ModelsState & { ok: boolean }
  >;
export const setModel = (pai: string, provider: string, model: string) =>
  post("/api/models", { pai, provider, model });
export const setApiKey = (provider: string, key: string) =>
  post("/api/apikey", { provider, key });
```

(If api.ts already has a types import line, extend it instead of duplicating.)

- [ ] **Step 3: ModelPicker.tsx** — create with this full content:

```tsx
import { useEffect, useMemo, useState } from "react";
import * as api from "../api";
import type { ModelRow, ModelsState } from "../types";

// Per-PAI provider/model picker. Reached from the chat-head "Model" button and
// cmd+k (it replaced the old CommandPalette, whose only commands were provider
// rows wired to a dead file). Key status is found/missing only — the server
// never sends key material.
export function ModelPicker({
  pai,
  onClose,
  onStatus,
  onSwitched,
}: {
  pai: string;
  onClose: () => void;
  onStatus: (text: string) => void;
  onSwitched: () => void;
}) {
  const [data, setData] = useState<ModelsState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  // Row awaiting input: a key for `${provider}/${model}` rows, or the custom row.
  const [expanded, setExpanded] = useState<string | null>(null);
  const [keyInput, setKeyInput] = useState("");
  const [customModel, setCustomModel] = useState("");
  const [busy, setBusy] = useState(false);

  const load = () => {
    api
      .getModels(pai)
      .then(setData)
      .catch((e) => setError(String(e?.message ?? e)));
  };
  useEffect(load, [pai]);

  const rows = useMemo(() => {
    const q = query.toLowerCase();
    return (data?.rows ?? []).filter(
      (r) => !q || r.label.toLowerCase().includes(q) || r.model.toLowerCase().includes(q),
    );
  }, [data, query]);

  const rowId = (r: ModelRow) => `${r.provider}/${r.model}`;
  const isActive = (r: ModelRow) =>
    data?.current?.provider === r.provider && data?.current?.model === r.model;

  const apply = async (provider: string, model: string, label: string, key?: string) => {
    setBusy(true);
    setError(null);
    try {
      if (key) await api.setApiKey(provider, key);
      await api.setModel(pai, provider, model);
      onStatus(`${pai}: switched to ${label} — takes effect next turn`);
      onSwitched();
      onClose();
    } catch (e: any) {
      setError(String(e?.message ?? e));
      setBusy(false);
    }
  };

  const pick = (r: ModelRow) => {
    if (busy) return;
    if (r.key_status === "found") {
      void apply(r.provider, r.model, r.label);
    } else {
      setKeyInput("");
      setExpanded(expanded === rowId(r) ? null : rowId(r));
    }
  };

  const keyEnv = (provider: string) => data?.providers[provider]?.api_key_env ?? "API key";
  const customNeedsKey = data?.providers["openrouter"]?.key_status === "missing";

  return (
    <div className="palette-overlay" onClick={onClose}>
      <div className="palette" onClick={(e) => e.stopPropagation()}>
        <div className="picker-title">
          Model — {pai}
          {data?.current && <span className="picker-current">{data.current.model}</span>}
        </div>
        <input
          className="palette-input"
          placeholder="Filter models…"
          autoFocus
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Escape") onClose();
          }}
        />
        <div className="palette-list">
          {rows.map((r) => (
            <div key={rowId(r)}>
              <button className="palette-item" disabled={busy} onClick={() => pick(r)}>
                <span className="palette-cmd">
                  {r.label}
                  {r.tag && <span className="model-tag">{r.tag}</span>}
                </span>
                <span className="palette-help">
                  {isActive(r) ? (
                    <span className="model-active">● active</span>
                  ) : r.key_status === "found" ? (
                    "key found"
                  ) : (
                    <span className="model-need-key">need key</span>
                  )}
                </span>
              </button>
              {expanded === rowId(r) && (
                <form
                  className="model-key-form"
                  onSubmit={(e) => {
                    e.preventDefault();
                    if (keyInput.trim()) void apply(r.provider, r.model, r.label, keyInput);
                  }}
                >
                  <input
                    className="palette-input"
                    type="password"
                    placeholder={`${keyEnv(r.provider)}…`}
                    autoFocus
                    value={keyInput}
                    onChange={(e) => setKeyInput(e.target.value)}
                  />
                  <button type="submit" className="head-action" disabled={busy || !keyInput.trim()}>
                    Save & switch
                  </button>
                </form>
              )}
            </div>
          ))}
          {data && (
            <div>
              <button
                className="palette-item"
                disabled={busy}
                onClick={() => {
                  setKeyInput("");
                  setExpanded(expanded === "custom" ? null : "custom");
                }}
              >
                <span className="palette-cmd">OpenRouter · custom model…</span>
                <span className="palette-help">
                  {customNeedsKey ? <span className="model-need-key">need key</span> : "any slug"}
                </span>
              </button>
              {expanded === "custom" && (
                <form
                  className="model-key-form"
                  onSubmit={(e) => {
                    e.preventDefault();
                    const model = customModel.trim();
                    if (!model) return;
                    void apply(
                      "openrouter",
                      model,
                      model,
                      customNeedsKey ? keyInput : undefined,
                    );
                  }}
                >
                  <input
                    className="palette-input"
                    placeholder="vendor/model — e.g. moonshotai/kimi-k2:free"
                    autoFocus
                    value={customModel}
                    onChange={(e) => setCustomModel(e.target.value)}
                  />
                  {customNeedsKey && (
                    <input
                      className="palette-input"
                      type="password"
                      placeholder="OPENROUTER_API_KEY…"
                      value={keyInput}
                      onChange={(e) => setKeyInput(e.target.value)}
                    />
                  )}
                  <button
                    type="submit"
                    className="head-action"
                    disabled={busy || !customModel.trim() || (customNeedsKey && !keyInput.trim())}
                  >
                    {customNeedsKey ? "Save & switch" : "Switch"}
                  </button>
                </form>
              )}
            </div>
          )}
          {data && !rows.length && <div className="palette-empty">no matches</div>}
          {!data && !error && <div className="palette-empty">loading…</div>}
          {error && <div className="palette-empty model-error">{error}</div>}
        </div>
      </div>
    </div>
  );
}
```

- [ ] **Step 4: App.tsx wiring.**
  - Replace `import { CommandPalette } from "./components/CommandPalette";` with `import { ModelPicker } from "./components/ModelPicker";` and add `ModelsState` to the types import.
  - Delete `const [provider, setProvider] = useState("anthropic");`, the `setProvider(msg.provider);` line in the `hello` case, the whole `case "provider": …` branch, and the `onPickProvider` callback.
  - Add near the other state hooks:

```tsx
  const [models, setModels] = useState<ModelsState | null>(null);
```

  - `activeMember` already exists (used by `activeLabel` ~line 868). Add below it:

```tsx
  const activeSlug = activeMember?.slug ?? null;
  const refreshModels = useCallback(() => {
    if (!activeSlug) {
      setModels(null);
      return;
    }
    api.getModels(activeSlug).then(setModels).catch(() => setModels(null));
  }, [activeSlug]);
  useEffect(refreshModels, [refreshModels]);

  const currentModelLabel = useMemo(() => {
    const cur = models?.current;
    if (!cur) return "Model";
    const row = models?.rows.find(
      (r) => r.provider === cur.provider && r.model === cur.model,
    );
    return row?.label ?? cur.model;
  }, [models]);
```

  (If `activeMember` is declared *after* the keyboard-shortcut effect, place these hooks after `activeMember`'s declaration — hooks order just has to be stable.)
  - Rename the `paletteOpen` state to `pickerOpen` (`const [pickerOpen, setPickerOpen] = useState(false);`) and update the cmd+k handler and Escape branch accordingly. In the cmd+k branch, only open when a fleet PAI is active: `if (activeSlug) setPickerOpen((v) => !v);` — reading `activeSlug` means adding it to that effect's dependency array.
  - In `chat-head-actions`, insert before the Clear button:

```tsx
                <button
                  className="head-action model-button"
                  type="button"
                  disabled={!activeMember}
                  onClick={() => setPickerOpen(true)}
                  title="Switch this PAI's provider/model (⌘K)"
                >
                  {currentModelLabel}
                </button>
```

  - Replace the `{paletteOpen && (<CommandPalette …/>)}` block with:

```tsx
      {pickerOpen && activeSlug && (
        <ModelPicker
          pai={activeSlug}
          onClose={() => setPickerOpen(false)}
          onStatus={setStatus}
          onSwitched={refreshModels}
        />
      )}
```

  - Delete `src/usr/libexec/web/src/components/CommandPalette.tsx`.

- [ ] **Step 5: styles.css.** Append (match the file's existing custom-property idiom — check how `.palette` colors are declared and reuse those variables):

```css
/* Model picker (evolves the old palette shell) */
.picker-title {
  padding: 10px 14px 0;
  font-size: 13px;
  font-weight: 600;
  display: flex;
  justify-content: space-between;
  gap: 8px;
}
.picker-current {
  font-weight: 400;
  opacity: 0.6;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.model-tag {
  margin-left: 6px;
  padding: 1px 5px;
  border: 1px solid currentColor;
  border-radius: 3px;
  font-size: 10px;
  text-transform: uppercase;
  opacity: 0.7;
}
.model-active { color: var(--ok, #3a9d5d); }
.model-need-key { opacity: 0.55; font-style: italic; }
.model-error { color: var(--warn, #b3564d); }
.model-key-form {
  display: flex;
  gap: 6px;
  padding: 4px 10px 8px;
}
.model-key-form .palette-input { flex: 1; }
.model-button {
  max-width: 160px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
```

(If `--ok`/`--warn` variables don't exist, substitute the concrete colors used by comparable state text in this stylesheet — e.g. the `.state-label` ready/busy colors.)

- [ ] **Step 6: Build to verify**

Run: `cd src/usr/libexec/web && npm install && npm run build`
Expected: clean tsc + vite build. A leftover `provider`/`setProvider`/`CommandPalette` reference anywhere fails compilation here — fix until clean. Also run `rg -n "setProvider|CommandPalette" src/usr/libexec/web/src/` — expect zero hits.

- [ ] **Step 7: Commit**

```bash
git add -A src/usr/libexec/web/src
git commit -m "web: ModelPicker dialog (per-PAI provider/model + key entry); retire CommandPalette"
```

---

### Task 7: full verification, merge, release

- [ ] **Step 1: Full test suite in the worktree**

Run: `uv run python -m pytest`
Expected: all green (repo was 332 passed / 2 skipped baseline; new tests add to that). Fix anything red before proceeding.

- [ ] **Step 2: End-to-end smoke (manual, from the worktree if the live fleet is up).** With the kernel and `pai start` running: open the console, click the Model button, switch the active PAI to a provider whose key exists, confirm the status line and that `~/.pai/etc/config.yaml` shows the new provider/model and `kernel.log` shows `reload_config: requested by web {action: set-model…}`. Then enter a bogus-but-well-formed OpenRouter key via a `need key` row, confirm `.env` gained `OPENROUTER_API_KEY` with mode 600. Switch the PAI back afterwards.

- [ ] **Step 3: Merge to main + push.** From the main checkout (which has the unrelated dirty files — do NOT commit them):

```bash
git -C ~/Projects/pai fetch
git -C <worktree> rebase origin/main
git -C ~/Projects/pai merge --ff-only <branch>   # or push the branch and merge remotely
git -C ~/Projects/pai push
```

If `origin/main` moved (the concurrent cowork workstream may have landed), resolve rebase conflicts — expected only as adjacent-line conflicts in `actions.py` imports and `test_config.py` appends.

- [ ] **Step 4: Release + deploy live (standing workflow)**

```bash
uv run pairelease --publish
pai update
sbin/reboot   # from ~/.pai
```

Verify the kernel re-exec in `~/.pai/var/log/kernel.log` and that the console (which self-re-execs on release skew) serves the new frontend.

- [ ] **Step 5: Clean up the worktree** (`git worktree remove …`).
