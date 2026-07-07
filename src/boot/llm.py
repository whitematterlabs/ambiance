"""Thin Anthropic SDK wrapper — system prompt + user turn + tool loop.

Runs the tool-call loop until the model stops calling tools. Returns
the final assistant text (may be empty if PAI chose not to respond).

Provider + model are passed in per call (ultimately sourced from each
PAI's `spec.yaml`, which is reconciled from `etc/config.yaml`). Clients
are cached by provider key — different PAIs on the same provider share
one HTTP client.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import httpx
from anthropic import AsyncAnthropic

from . import tokens

from . import bash_tool, inject, noop_tool, shell_tool, stitch
from .image_refs import expand_image_refs
from . import paths as paths_mod
from .paths import HOME_DIR
from .processes import ProcessNotFound, append_log

MAX_TOKENS = 4096
MAX_ITERATIONS = 200

# Max characters of a single tool result fed back to the model. A command that
# dumps megabytes (cat huge.log, find /, a runaway loop) would otherwise inflate
# one turn's prompt past the provider's context window — the very next
# messages.create in the loop 400s ("maximum context length ... requested N
# tokens"). That failure is unrecoverable by the history-reset overflow path
# (nudge._emergency_reset_history): the bloat is generated *within* the turn, so
# the retry reproduces it and dies fatally, reaping any running subagent. Capping
# the model-bound copy head+tail guarantees one tool call can never blow the
# window. The full output is still printed/logged to the TTY — only the copy
# sent back to the model is truncated. ~50K tokens at ~4 chars/token.
MAX_TOOL_RESULT_CHARS = 200_000


def _cap_tool_result(text: str) -> str:
    """Truncate an oversized tool result to a head+tail with an elision marker.
    Strings within budget pass through unchanged."""
    if len(text) <= MAX_TOOL_RESULT_CHARS:
        return text
    half = MAX_TOOL_RESULT_CHARS // 2
    elided = len(text) - 2 * half
    marker = (
        f"\n\n[... {elided} characters elided by kernel — tool output exceeded "
        f"the {MAX_TOOL_RESULT_CHARS}-char cap; the full output is in the "
        f"TTY/log. Re-run narrowed (head/tail/grep/sed -n) to see more ...]\n\n"
    )
    return text[:half] + marker + text[-half:]


def _narrate(pai_slug: str, text: str) -> None:
    """Surface interim assistant text live: TTY, proc log, and the owner's
    me-thread. Used for text blocks emitted alongside tool calls and for a
    would-be final reply that an injected message pre-empts."""
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        print(f"[pai:{pai_slug}] » {line}", flush=True)
        try:
            append_log(pai_slug, f"» {line}")
        except ProcessNotFound:
            pass
        _append_narration_to_me_thread(pai_slug, line)


def _append_narration_to_me_thread(pai_slug: str, line: str) -> None:
    """Mirror interim text-block narration into the owner-facing me/ thread
    so the web console / ChatPane shows it inline. Body is prefixed with `» `
    to distinguish it from a final reply. Keyed by slug, not pid — see
    `paths.me_thread_dir`."""
    path = paths_mod.me_thread_today(pai_slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    hm = datetime.now().strftime("%H:%M")
    with path.open("a", encoding="utf-8") as f:
        f.write(f"[{hm}] pai: » {line}\n")

# TCP port the kernel-supervised LiteLLM proxy listens on (loopback only).
# Shared source of truth: the `openai` provider row below builds its base_url
# from it, and boot/litellm_proxy.py binds + polls readiness against it.
PROXY_PORT = 4000

# Wall-clock guard on a single model HTTP call. The loopback hop (kernel→proxy)
# is fast; the real risk is the proxy→upstream hop stalling. read=300s sits
# above LiteLLM's own request_timeout*(num_retries+1) (see litellm_proxy.py) so
# the proxy returns a clean error/retry before this raw timeout fires; this is
# the backstop that turns a wedged upstream into a ~5-min failure, not ~10.
_CLIENT_TIMEOUT = httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=10.0)


@dataclass(frozen=True)
class ProviderSpec:
    """How to reach one LLM provider over the Anthropic Messages wire.

    `via_proxy` is *not* read by `_resolve` — the hot tool loop stays
    branch-free: a proxied provider just carries a loopback `base_url` like
    any Anthropic-compatible endpoint. It is read only by the kernel
    (boot/litellm_proxy.py) to decide whether to spawn the LiteLLM proxy.
    """

    base_url: Optional[str]
    api_key_env: str
    default_model: str
    extra_body: dict = field(default_factory=dict)
    via_proxy: bool = False
    # Only meaningful when via_proxy=True: how the LiteLLM proxy reaches the
    # real upstream. proxy_prefix is LiteLLM's provider id (e.g. "openai");
    # proxy_api_base overrides the upstream URL (None = LiteLLM's default for
    # that prefix). _resolve namespaces the wire model with proxy_prefix so the
    # proxy's per-provider model_list row can route it; _write_config emits one
    # row per proxied provider from these fields.
    proxy_prefix: Optional[str] = None
    proxy_api_base: Optional[str] = None


# provider key -> ProviderSpec
PROVIDERS: dict[str, ProviderSpec] = {
    "anthropic": ProviderSpec(None, "ANTHROPIC_API_KEY", "claude-sonnet-4-6", {}),
    # Deepseek's Anthropic-compatible endpoint defaults thinking=enabled.
    # Thinking blocks are stripped from replies (text-only extraction at line
    # ~200) and preserved in tool-call history via model_dump(), so no extra
    # body override is needed.
    "deepseek": ProviderSpec(
        "https://api.deepseek.com/anthropic",
        "DEEPSEEK_API_KEY",
        "deepseek-v4-pro",
        {},
    ),
    # z.ai's GLM via its Anthropic-compatible endpoint — same shape as DeepSeek:
    # Anthropic-wire-native, reached directly, no proxy. The default is glm-5.2;
    # pin model: glm-5.2[1m] in config for the 1M-context variant. The key is
    # sent as the api_key kwarg (x-api-key header), which the endpoint accepts.
    "zai": ProviderSpec(
        "https://api.z.ai/api/anthropic",
        "ZAI_API_KEY",
        "glm-5.2",
        {},
    ),
    # OpenAI is not Anthropic-wire-compatible, so it routes through the
    # kernel-supervised LiteLLM proxy, which exposes a native Anthropic
    # /v1/messages endpoint on loopback. The client still speaks Anthropic;
    # the proxy translates to the OpenAI API and back. via_proxy=True tells
    # the kernel to spawn that proxy when an openai PAI is in the fleet.
    "openai": ProviderSpec(
        f"http://127.0.0.1:{PROXY_PORT}",
        "OPENAI_API_KEY",
        "gpt-5.5",
        {},
        via_proxy=True,
        proxy_prefix="openai",
    ),
}
DEFAULT_PROVIDER = "anthropic"

# provider_key -> AsyncAnthropic client (one per provider).
_clients: dict[str, AsyncAnthropic] = {}


def _resolve(provider: Optional[str], model: Optional[str]) -> tuple[AsyncAnthropic, str, dict]:
    """Return (client, model, extra_body) for a (provider, model) pair.

    Both args may be None: provider falls back to DEFAULT_PROVIDER; model
    falls back to the provider's default. Unknown providers raise.

    Model ids may carry an OpenRouter-style `provider/` prefix
    (e.g. `anthropic/claude-opus-4-7`). The prefix is informational —
    routing is decided by the `provider` field — so we split it off
    before sending to the API."""
    key = provider or DEFAULT_PROVIDER
    if key not in PROVIDERS:
        raise ValueError(f"unknown provider: {key!r}")
    spec = PROVIDERS[key]
    base_url = spec.base_url
    api_key_env = spec.api_key_env
    default_model = spec.default_model
    extra_body = spec.extra_body
    # Normalize any incoming OpenRouter-style prefix to a bare model id.
    if model and "/" in model:
        model = model.split("/", 1)[1]
    client = _clients.get(key)
    if client is None:
        api_key = os.environ.get(api_key_env)
        kwargs: dict = {"timeout": _CLIENT_TIMEOUT}
        if api_key:
            kwargs["api_key"] = api_key
        if base_url:
            kwargs["base_url"] = base_url
        client = AsyncAnthropic(**kwargs)
        _clients[key] = client
    wire_model = model or default_model
    # Proxied providers share one loopback endpoint; the proxy disambiguates by
    # the LiteLLM provider namespace, so send "<prefix>/<model>" (e.g.
    # "openai/gpt-5.5"). Direct providers send the bare model unchanged.
    if spec.via_proxy and spec.proxy_prefix:
        wire_model = f"{spec.proxy_prefix}/{wire_model}"
    return client, wire_model, extra_body


class TurnCancelled(Exception):
    """Raised when run_turn is cancelled mid-loop.

    Carries the partial message list, pruned of any trailing assistant
    turn with unresolved tool_uses so it can be persisted and resumed.
    """

    def __init__(self, messages: list[dict]):
        super().__init__("turn cancelled")
        self.messages = messages


def _prune_unresolved_tool_uses(messages: list[dict]) -> None:
    """Drop trailing assistant turns whose tool_uses have no tool_results.

    After cancellation, the history may end with an assistant turn that
    requested tools we never ran. The Anthropic API rejects that shape on
    the next call. Pop such a trailing turn so the history ends cleanly.
    """
    while messages:
        last = messages[-1]
        if last.get("role") != "assistant":
            return
        blocks = last.get("content") or []
        has_tool_use = any(
            isinstance(b, dict) and b.get("type") == "tool_use" for b in blocks
        )
        if not has_tool_use:
            return
        messages.pop()


def _tool_result_base_dir(env: Optional[dict]) -> Path:
    """Base dir for resolving relative image refs in tool output.

    A PAI's bash/shell session runs with cwd = the PAI's home, so relative
    image paths it emits are relative to that home — not the kernel's launch
    cwd. Using the kernel cwd was also fragile: a long-running supervisor
    whose launch dir is later removed makes os.getcwd() raise
    FileNotFoundError, which would kill the whole turn (even tool output with
    no images at all).
    """
    raw_slug = (env or {}).get("PAI_SLUG")
    try:
        return stitch.home_for(raw_slug) if raw_slug else HOME_DIR
    except Exception:
        return HOME_DIR


async def run_turn(
    system: str,
    user: str,
    history: Optional[list[dict]] = None,
    env: Optional[dict] = None,
    *,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    set_status: Optional[callable] = None,
) -> tuple[str, list[dict]]:
    """Run one nudge through the model.

    `provider` and `model` come from the calling PAI's spec.yaml (which
    is reconciled from etc/config.yaml). Both default to the global
    fallback when omitted, so subagent code paths that don't carry a
    spec still work.

    Returns (final assistant text, full messages list after the turn).
    The returned list = `history` + the user turn + every assistant/
    tool_result turn the loop produced. Caller persists it.

    On cancellation raises TurnCancelled with the pruned partial history.
    """
    client, model, extra_body = _resolve(provider, model)
    messages: list[dict] = list(history) if history else []
    user_content = expand_image_refs(user, base_dir=HOME_DIR)
    messages.append({"role": "user", "content": user_content})

    try:
        return await _loop(client, model, extra_body, system, messages, env, set_status)
    except asyncio.CancelledError:
        _prune_unresolved_tool_uses(messages)
        raise TurnCancelled(messages)


def _with_cache_control(messages: list[dict]) -> list[dict]:
    """Mark the last message's last content block with cache_control so the
    Anthropic prompt cache covers everything up to here. Mutates a shallow
    copy so the on-disk history stays plain."""
    if not messages:
        return messages
    out = list(messages)
    last = dict(out[-1])
    content = last.get("content")
    # String content (typical for the freshly-appended user turn): wrap into
    # a single text block so we can attach cache_control.
    if isinstance(content, str):
        last["content"] = [
            {"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}
        ]
    elif isinstance(content, list) and content:
        new_content = list(content)
        tail = dict(new_content[-1])
        tail["cache_control"] = {"type": "ephemeral"}
        new_content[-1] = tail
        last["content"] = new_content
    out[-1] = last
    return out


async def _loop(
    client: AsyncAnthropic,
    model: str,
    extra_body: dict,
    system: str,
    messages: list[dict],
    env: Optional[dict],
    set_status: Optional[callable] = None,
) -> tuple[str, list[dict]]:
    def _status(reason: str) -> None:
        if set_status is not None:
            try:
                set_status(reason)
            except Exception:
                pass
    # System prompt is static across nudges — cache it. The tail of `messages`
    # is also marked per-iteration so the growing history reuses the cached
    # prefix on subsequent iterations and subsequent nudges (within the
    # ephemeral 5-minute TTL).
    system_blocks = [
        {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
    ]
    short_model = model.split("/")[-1] if "/" in model else model
    for _ in range(MAX_ITERATIONS):
        _status(f"waiting on {short_model}")
        response = await client.messages.create(
            model=model,
            max_tokens=MAX_TOKENS,
            system=system_blocks,
            tools=[
                bash_tool.TOOL_SCHEMA,
                shell_tool.TOOL_SCHEMA,
                noop_tool.TOOL_SCHEMA,
            ],
            messages=_with_cache_control(messages),
            extra_body=extra_body,
        )
        tokens.record((env or {}).get("PAI_SLUG"), short_model, response.usage)

        # Anthropic SDK returns content blocks as objects; serialize for
        # on-disk history so we can round-trip via JSON.
        messages.append({
            "role": "assistant",
            "content": [b.model_dump() for b in response.content],
        })

        pai_slug = (env or {}).get("PAI_SLUG")
        tool_uses = [b for b in response.content if b.type == "tool_use"]
        if not tool_uses:
            text_parts = [b.text for b in response.content if b.type == "text"]
            reply = "\n".join(text_parts).strip()
            # A message was injected mid-generation (see boot.inject): the
            # turn doesn't end — the pending message becomes the next user
            # input and the loop continues. The would-be final reply is
            # narrated so it still reaches the owner/log instead of being
            # swallowed by the continuation.
            pending = inject.drain(pai_slug)
            if pending:
                if pai_slug and reply and not noop_tool.is_sentinel_text(reply):
                    _narrate(pai_slug, reply)
                messages.append({
                    "role": "user",
                    "content": [{"type": "text", "text": t} for t in pending],
                })
                continue
            # A quiet turn the model expressed as prose ("stand_down", "NOOP",
            # "quiet") instead of calling the stand_down tool. Canonicalize the
            # sentinel to no reply so it never surfaces as a bogus message.
            if noop_tool.is_sentinel_text(reply):
                return "", messages
            return reply, messages

        # Surface interim narration: text blocks emitted alongside tool_uses
        # are the model "thinking out loud" between actions. They'd otherwise
        # vanish into history. Append to the PAI's log so the TUI / tail can
        # pick them up live. One-shot per block, prefixed for legibility.
        noop_only = all(use.name == noop_tool.TOOL_NAME for use in tool_uses)
        if noop_only:
            # The model may include filler text alongside stand_down ("Quiet.",
            # "Nothing to do."). Keep it out of live narration and history.
            assistant_turn = messages[-1]
            assistant_turn["content"] = [
                block
                for block in assistant_turn.get("content", [])
                if isinstance(block, dict) and block.get("type") == "tool_use"
            ]
        if pai_slug and not noop_only:
            for block in response.content:
                if block.type != "text":
                    continue
                text = (block.text or "").strip()
                if text:
                    _narrate(pai_slug, text)

        tool_results = []
        for use in tool_uses:
            if use.name == bash_tool.TOOL_NAME:
                pai_slug = (env or {}).get("PAI_SLUG") or "?"
                command = use.input.get("command", "")
                print(f"[pai:{pai_slug}] $ {command}", flush=True)
                _status(f"bash: {command}"[:120])
                result = await bash_tool.run(use.input, env=env)
                rendered = result.render()
                print(rendered, flush=True)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": use.id,
                    "content": expand_image_refs(
                        _cap_tool_result(rendered),
                        base_dir=_tool_result_base_dir(env),
                    ),
                })
            elif use.name == shell_tool.TOOL_NAME:
                pai_slug = (env or {}).get("PAI_SLUG") or "?"
                if use.input.get("keys"):
                    keys_repr = use.input["keys"]
                    print(f"[pai:{pai_slug}] [keys] {keys_repr}", flush=True)
                    _status(f"shell: send-keys {keys_repr}"[:120])
                else:
                    command = use.input.get("command", "")
                    print(f"[pai:{pai_slug}] $ {command}", flush=True)
                    _status(f"shell: {command}"[:120])
                result = await shell_tool.run(use.input, env=env)
                rendered = result.render()
                print(rendered, flush=True)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": use.id,
                    "content": expand_image_refs(
                        _cap_tool_result(rendered),
                        base_dir=_tool_result_base_dir(env),
                    ),
                })
            elif use.name == noop_tool.TOOL_NAME:
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": use.id,
                    "content": noop_tool.TOOL_RESULT,
                })
            else:
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": use.id,
                    "content": f"unknown tool: {use.name}",
                    "is_error": True,
                })

        messages.append({"role": "user", "content": tool_results})
        # Tool-boundary injection (see boot.inject): messages that arrived
        # while the model was generating or the tools were running ride along
        # in the same user turn, as text blocks after the tool_results.
        pending = inject.drain(pai_slug)
        for t in pending:
            messages[-1]["content"].append({"type": "text", "text": t})
        if noop_only:
            if pending:
                # The model stood down, but new input just landed — the turn
                # continues so the message is handled now, not next wake.
                continue
            messages.append({
                "role": "assistant",
                "content": [{"type": "text", "text": noop_tool.TOOL_NAME}],
            })
            return "", messages

    return "[max iterations reached]", messages
