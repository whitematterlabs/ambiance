"""Thin Anthropic SDK wrapper — system prompt + user turn + tool loop.

Runs the tool-call loop until the model stops calling tools or we hit
the iteration cap. Returns the final assistant text (may be empty if
PAI chose not to respond).
"""

from __future__ import annotations

import asyncio
import os
from typing import Optional

import yaml
from anthropic import AsyncAnthropic

from . import shell_tool
from .processes import LIVE_DIR

MAX_TOKENS = 4096
MAX_ITERATIONS = 25

PROVIDER_CONFIG_PATH = LIVE_DIR / "memory" / "myself" / "provider.yaml"

# provider key -> (base_url or None, api_key env var, default model, extra_body)
_PROVIDERS: dict[str, tuple[Optional[str], str, str, dict]] = {
    "anthropic": (None, "ANTHROPIC_API_KEY", "claude-sonnet-4-6", {}),
    # Deepseek's Anthropic-compatible endpoint defaults thinking=enabled, which
    # demands thinking blocks be preserved in tool-call history. Our histories
    # don't contain them, so disable thinking entirely.
    "deepseek": (
        "https://api.deepseek.com/anthropic",
        "DEEPSEEK_API_KEY",
        "deepseek-v4-pro",
        {"thinking": {"type": "disabled"}},
    ),
}
DEFAULT_PROVIDER = "anthropic"


def _read_provider_key() -> str:
    try:
        data = yaml.safe_load(PROVIDER_CONFIG_PATH.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return DEFAULT_PROVIDER
    key = data.get("provider") if isinstance(data, dict) else None
    return key if key in _PROVIDERS else DEFAULT_PROVIDER


# Cached (provider_key, client, model, extra_body). Rebuilt on swap.
_cached: Optional[tuple[str, AsyncAnthropic, str, dict]] = None


def _get_client_and_model() -> tuple[AsyncAnthropic, str, dict]:
    global _cached
    key = _read_provider_key()
    if _cached is not None and _cached[0] == key:
        return _cached[1], _cached[2], _cached[3]
    base_url, api_key_env, default_model, extra_body = _PROVIDERS[key]
    api_key = os.environ.get(api_key_env)
    kwargs: dict = {}
    if api_key:
        kwargs["api_key"] = api_key
    if base_url:
        kwargs["base_url"] = base_url
    client = AsyncAnthropic(**kwargs)
    _cached = (key, client, default_model, extra_body)
    return client, default_model, extra_body


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


async def run_turn(
    system: str,
    user: str,
    history: Optional[list[dict]] = None,
    env: Optional[dict] = None,
) -> tuple[str, list[dict]]:
    """Run one nudge through the model.

    Returns (final assistant text, full messages list after the turn).
    The returned list = `history` + the user turn + every assistant/
    tool_result turn the loop produced. Caller persists it.

    On cancellation raises TurnCancelled with the pruned partial history.
    """
    client, model, extra_body = _get_client_and_model()
    messages: list[dict] = list(history) if history else []
    messages.append({"role": "user", "content": user})

    try:
        return await _loop(client, model, extra_body, system, messages, env)
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
) -> tuple[str, list[dict]]:
    # System prompt is static across nudges — cache it. The tail of `messages`
    # is also marked per-iteration so the growing history reuses the cached
    # prefix on subsequent iterations and subsequent nudges (within the
    # ephemeral 5-minute TTL).
    system_blocks = [
        {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
    ]
    for _ in range(MAX_ITERATIONS):
        response = await client.messages.create(
            model=model,
            max_tokens=MAX_TOKENS,
            system=system_blocks,
            tools=[shell_tool.TOOL_SCHEMA],
            messages=_with_cache_control(messages),
            extra_body=extra_body,
        )

        # Anthropic SDK returns content blocks as objects; serialize for
        # on-disk history so we can round-trip via JSON.
        messages.append({
            "role": "assistant",
            "content": [b.model_dump() for b in response.content],
        })

        tool_uses = [b for b in response.content if b.type == "tool_use"]
        if not tool_uses:
            text_parts = [b.text for b in response.content if b.type == "text"]
            return "\n".join(text_parts).strip(), messages

        tool_results = []
        for use in tool_uses:
            if use.name == shell_tool.TOOL_NAME:
                command = use.input.get("command", "")
                pai_slug = (env or {}).get("PAI_SLUG") or "?"
                print(f"[pai:{pai_slug}] $ {command}", flush=True)
                result = await shell_tool.run(command, env=env)
                rendered = result.render()
                print(rendered, flush=True)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": use.id,
                    "content": rendered,
                })
            else:
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": use.id,
                    "content": f"unknown tool: {use.name}",
                    "is_error": True,
                })

        messages.append({"role": "user", "content": tool_results})

    return "[max iterations reached]", messages
