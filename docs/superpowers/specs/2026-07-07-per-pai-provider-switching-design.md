# Per-PAI Provider Switching — Design

**Date:** 2026-07-07
**Status:** Approved

## Problem

Provider/API-key setup exists only in the install path (`install.sh`,
`paisetup/apikey.py`). The kernel already supports per-PAI providers —
`/etc/config.yaml` carries `provider:`/`model:` per PAI, `nudge.py` passes them
to `llm._resolve` every turn — but the owner has no runtime surface to use it.
The web console's cmd+k CommandPalette has provider rows, but they write
`memory/myself/provider.yaml`, which nothing in the kernel reads: a vestigial
TUI-era dead end. There is also no OpenRouter support, which matters because
its `:free` models are the cheapest way to try PAI.

## Goals

- Switch the **active PAI tab** to any supported provider+model from the web
  console, taking effect next turn, no restart.
- Enter a missing API key from the browser; the running kernel picks it up
  without a re-exec.
- Add OpenRouter as a provider (via the existing LiteLLM proxy), with curated
  free models in the catalog.
- Delete the vestigial `provider.yaml` plumbing rather than extend it.

## Non-goals

- Live-fetching model lists from provider APIs.
- Fleet-wide bulk switching (edit `config.yaml` by hand for that).
- Key management beyond add/replace (no delete/reveal UI; status is only ever
  found/missing — keys never round-trip to the browser).

## Design

### 1. Kernel catalog (`boot/llm.py`)

- Add `openrouter` to `PROVIDERS`: `via_proxy=True`,
  `proxy_prefix="openrouter"`, `api_key_env="OPENROUTER_API_KEY"`,
  `base_url=http://127.0.0.1:{PROXY_PORT}`, default model = a curated free
  model. LiteLLM's `openrouter/*` wildcard row (emitted by
  `litellm_proxy._write_config`, unchanged shape) routes it upstream.
- **Fix `_resolve` prefix-stripping.** Today any `vendor/` prefix on a model id
  is stripped as informational. OpenRouter model ids are legitimately
  `vendor/model` slugs (`moonshotai/kimi-k2:free`), so stripping would mangle
  them. New rule: strip a leading `<provider-key>/` or `<proxy_prefix>/` only
  (self-referential prefix, e.g. `anthropic/claude-…` on the anthropic
  provider); otherwise the model id passes through intact. Proxied providers
  still get `{proxy_prefix}/` prepended for the wire, yielding
  `openrouter/moonshotai/kimi-k2:free` — a shape LiteLLM's `openrouter/*`
  wildcard matches.
- Add `CATALOG: list[CatalogEntry]` — the curated provider+model rows, single
  source of truth imported by the web backend (same pattern as `paisetup`
  importing `PROVIDERS`). Fields: `provider`, `model`, `label`, `tag`
  (e.g. `"free"`, `None`). Initial rows:
  - DeepSeek V4-Pro (`deepseek` / `deepseek-v4-pro`)
  - Claude Opus 4.8 (`anthropic` / `claude-opus-4-8`)
  - Claude Sonnet 4-6 (`anthropic` / `claude-sonnet-4-6`)
  - ChatGPT-5.5 (`openai` / `gpt-5.5`)
  - GLM 5.2 (`zai` / `glm-5.2`)
  - 2–3 OpenRouter `:free` rows (picked at implementation time from what's
    currently listed, e.g. Kimi K2 free)

### 2. Key pickup on `kernel:reload_config`

`_handle_reload_config` additionally:

1. Re-loads `$PAI_ROOT/.env.local` / `.env` (and the dev-root pair) with
   `override=True`, mirroring the boot-time precedence in `boot/__init__.py`.
2. Clears `llm._clients` wholesale (≤5 entries; no per-key finesse needed), so
   the next turn constructs clients with the fresh env.

Proxy reconcile handles the rest, but needs more than spawn/stop: a running
proxy freezes its config and env at fork. `litellm_proxy.reconcile(event)`
therefore also, when the proxy is needed and already running, regenerates the
config and restarts the proxy if the content changed (e.g. a PAI switched to a
new proxied provider), and restarts even on unchanged config when the reload
event is a `set-api-key` for a proxied provider — the key resolves from the
proxy's own process env, so only a restart picks it up.

### 3. Web backend (`pai_web`)

Three endpoints (all under the existing `/api/*` auth gate):

- `GET /api/models` → `{rows: [{provider, model, label, tag, key_status}],
  current: {pai, provider, model}}`. `key_status` per provider = found/missing,
  probing process env then `$PAI_ROOT/.env.local`/`.env` (reuse
  `paisetup/apikey.py`'s `_key_already_present` logic — lift it into a shared
  helper rather than duplicating). `current` reads the active PAI's entry in
  `/etc/config.yaml` (the source of truth the POST edits — so a re-fetch right
  after a switch is never stale), resolving absent provider/model to
  `DEFAULT_PROVIDER`/the provider's default the same way reconcile does.
- `POST /api/models` `{pai, provider, model}` → validate provider against
  `PROVIDERS` (model is free-form — custom row), rewrite that PAI's entry in
  `/etc/config.yaml` (yaml round-trip, write-temp-then-rename), emit
  `kernel:reload_config`. Config stays the source of truth; reconcile rewrites
  `spec.yaml`.
- `POST /api/apikey` `{provider, key}` → resolve `api_key_env` from
  `PROVIDERS`, replace-or-append `VAR=value` in `$PAI_ROOT/.env` (chmod 600),
  set it in the web server's own `os.environ` too (voice/actions probe env
  directly), emit `kernel:reload_config`. Response carries only
  `{ok, key_status: "found"}`.

**Deleted:** `read_provider`/`write_provider`/`PROVIDER_CONFIG_PATH`/
`PROVIDER_OPTIONS` in `actions.py`, the `POST /api/provider` route, the
`provider` field in `hub.snapshot()` and the `provider` SSE broadcast, and the
`provider.yaml` file's role. `HUB.snapshot()` loses its argument.

**Note on dependents:** subagents inherit the spawning PAI's provider/model
unless a `--model` or bundle pin overrides it (`subagent._inherited_model`
cascade) — switching a PAI also switches what its future subagents inherit.
That is the intended semantic; the dialog does not surface it. (Persubs and
the `dependencies:` config key were removed 2026-07-07.)

### 4. Frontend (`src/usr/libexec/web`)

New `ModelPicker.tsx` (evolves CommandPalette's filterable-row UI;
`CommandPalette.tsx` is deleted — provider rows were its only commands):

```
┌─ Models — pai ──────────────────┐
│ (filter…)                       │
│ DeepSeek V4-Pro        ● active │
│ Claude Opus 4.8     [key found] │
│ Claude Sonnet 4-6   [key found] │
│ ChatGPT-5.5          [need key] │
│ GLM 5.2             [key found] │
│ OpenRouter · Kimi K2 (free)     │
│                      [need key] │
│ OpenRouter · custom model…      │
└─────────────────────────────────┘
```

- Opens from a **Model** button in `chat-head-actions` (next to Clear/Compact,
  label = short name of the active PAI's current model) and from cmd+k.
- Dialog title names the active PAI; a switch affects only it.
- Fetches `GET /api/models` on open (fresh key status; no SSE dependency).
- Row with key found → click applies immediately; status line: "switched to
  <label> — takes effect next turn".
- Row with key missing → inline password-type input expands under the row;
  save POSTs the key, then applies the switch in the same flow.
- `OpenRouter · custom model…` row → free-text model id input (plus the key
  input if the OpenRouter key is missing).
- Active row marked `● active`; re-fetched after a successful switch.

### 5. Error handling

- `POST /api/models` with unknown provider or unknown PAI → 400 with message;
  dialog shows it inline.
- `config.yaml` edit failures (parse error from hand-edits) → 500 with the
  YAML error; nothing written (temp-file rename only on success).
- Empty/whitespace key → 400, input kept open.
- Kernel not running: endpoints still work (config/.env are files); the switch
  simply applies when the kernel next boots. No special casing.

### 6. Testing

- `llm._resolve`: openrouter slug pass-through (`moonshotai/kimi-k2:free` →
  wire `openrouter/moonshotai/kimi-k2:free`), self-prefix stripping unchanged
  (`anthropic/claude-x` → `claude-x`), unknown provider still raises.
- Catalog/key-probe: `key_status` from env vs `.env` vs missing.
- `.env` write: replace-vs-append, chmod 600 preserved, round-trip.
- `config.yaml` per-PAI edit: only the target PAI's entry changes; comments…
  are not preserved by yaml round-trip — acceptable (paiadd already rewrites
  via yaml); malformed yaml → error, file untouched.
- Web routes: auth-gated, happy path + 400s, via the existing web test harness.
- Reload: `_handle_reload_config` clears `llm._clients` and re-reads a mutated
  `.env` (override semantics).

## Out of scope / follow-ups

- Surfacing per-model pricing or context limits in the dialog.
- A "test key" probe (round-trip a 1-token completion) before saving.
- install.sh gaining OpenRouter as a seed-provider choice (worth doing, small,
  separate change).
