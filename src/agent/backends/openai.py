"""OpenAI-wire backend — Chat Completions, tool_calls, role:"tool".

Owns its history shape: assistant turns carry `tool_calls` (arguments
as JSON strings), each answered by a `role: "tool"` message keyed by
`tool_call_id`. The system prompt is prepended per call and never
persisted. Prompt caching is the provider's automatic prefix cache —
nothing to annotate. Image refs stay as text paths at v4.0 (no
image_url expansion yet).

History persisted by this backend is only ever replayed through this
backend; a provider switch across wire families goes through the turn
engine's compact-and-reseed, never through translation.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Callable, Optional

import httpx
from openai import AsyncOpenAI

from .. import tokens
from . import base
from .base import ProviderSpec, TurnCancelled

_CLIENT_TIMEOUT = httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=10.0)

_clients: dict[str, AsyncOpenAI] = {}


def _client(key: str, spec: ProviderSpec) -> AsyncOpenAI:
    client = _clients.get(key)
    if client is None:
        kwargs: dict = {"timeout": _CLIENT_TIMEOUT}
        if api_key := os.environ.get(spec.api_key_env):
            kwargs["api_key"] = api_key
        if spec.base_url:
            kwargs["base_url"] = spec.base_url
        client = AsyncOpenAI(**kwargs)
        _clients[key] = client
    return client


def _tool_schemas() -> list[dict]:
    """Render the shared Anthropic-shaped tool schemas into function tools.
    Same JSON Schema either way — only the wrapper differs."""
    return [
        {
            "type": "function",
            "function": {
                "name": s["name"],
                "description": s["description"],
                "parameters": s["input_schema"],
            },
        }
        for s in base.TOOL_SCHEMAS
    ]


def _prune_unresolved_tool_calls(messages: list[dict]) -> None:
    while messages:
        last = messages[-1]
        if last.get("role") != "assistant" or not last.get("tool_calls"):
            return
        messages.pop()


def _usage_dict(usage) -> dict:
    """Map OpenAI usage onto the counter names tokens.py rolls up."""
    if usage is None:
        return {}
    prompt = getattr(usage, "prompt_tokens", 0) or 0
    cached = getattr(
        getattr(usage, "prompt_tokens_details", None), "cached_tokens", 0
    ) or 0
    return {
        "input_tokens": prompt - cached,
        "output_tokens": getattr(usage, "completion_tokens", 0) or 0,
        "cache_read_input_tokens": cached,
        "cache_creation_input_tokens": 0,
    }


async def run_turn(
    provider_key: str,
    spec: ProviderSpec,
    model: str,
    system: str,
    user: str,
    history: Optional[list[dict]] = None,
    *,
    state_dir: Optional[Path] = None,
    home: Optional[Path] = None,
    drain: Optional[Callable[[], list[str]]] = None,
    client=None,
) -> tuple[str, list[dict]]:
    client = client or _client(provider_key, spec)
    messages: list[dict] = list(history) if history else []
    messages.append({"role": "user", "content": user})
    try:
        return await _loop(client, spec, model, system, messages, state_dir, drain)
    except asyncio.CancelledError:
        _prune_unresolved_tool_calls(messages)
        raise TurnCancelled(messages)


async def _loop(
    client,
    spec: ProviderSpec,
    model: str,
    system: str,
    messages: list[dict],
    state_dir: Optional[Path],
    drain: Optional[Callable[[], list[str]]],
) -> tuple[str, list[dict]]:
    def _drain() -> list[str]:
        return drain() if drain else []

    tools = _tool_schemas()
    for _ in range(base.MAX_ITERATIONS):
        response = await client.chat.completions.create(
            model=model,
            max_completion_tokens=base.MAX_TOKENS,
            messages=[{"role": "system", "content": system}] + messages,
            tools=tools,
            **(spec.extra_body or {}),
        )
        if state_dir is not None:
            tokens.record(state_dir, model, _usage_dict(response.usage))

        choice = response.choices[0].message
        tool_calls = list(choice.tool_calls or [])
        assistant: dict = {"role": "assistant", "content": choice.content or ""}
        if tool_calls:
            assistant["tool_calls"] = [
                {
                    "id": c.id,
                    "type": "function",
                    "function": {
                        "name": c.function.name,
                        "arguments": c.function.arguments,
                    },
                }
                for c in tool_calls
            ]
        messages.append(assistant)

        if not tool_calls:
            reply = (choice.content or "").strip()
            # New input landed mid-generation: the turn continues with it;
            # the would-be reply is narrated so it isn't swallowed.
            if pending := _drain():
                if reply and not base.is_noop_text(reply):
                    base.narrate(reply)
                messages.extend({"role": "user", "content": t} for t in pending)
                continue
            if base.is_noop_text(reply):
                return "", messages
            return reply, messages

        noop_only = all(c.function.name == base.NOOP_NAME for c in tool_calls)
        if noop_only:
            # Filler text alongside do_nothing stays out of narration and
            # history.
            assistant["content"] = ""
        elif text := (choice.content or "").strip():
            base.narrate(text)

        for call in tool_calls:
            try:
                tool_input = json.loads(call.function.arguments or "{}")
                if not isinstance(tool_input, dict):
                    raise ValueError("arguments must be an object")
            except (json.JSONDecodeError, ValueError) as e:
                content, is_error = f"malformed tool arguments: {e}", True
            else:
                content, is_error = await base.run_tool(call.function.name, tool_input)
            messages.append({
                "role": "tool",
                "tool_call_id": call.id,
                "content": ("ERROR: " if is_error else "") + content,
            })

        pending = _drain()
        messages.extend({"role": "user", "content": t} for t in pending)
        if noop_only:
            if pending:
                # The model stood down, but new input just landed — handle
                # it now, not next wake.
                continue
            messages.append({"role": "assistant", "content": base.NOOP_NAME})
            return "", messages

    return "[max iterations reached]", messages
