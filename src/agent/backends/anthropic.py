"""Anthropic-wire backend — Messages API, content blocks, cache_control.

Owns its history shape: assistant turns are lists of blocks
(text/tool_use), tool results ride a user turn as tool_result blocks.
Image refs expand into native image blocks. History persisted by this
backend is only ever replayed through this backend.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Callable, Optional

import httpx
from anthropic import AsyncAnthropic

from .. import tokens
from ..image_refs import expand_image_refs
from . import base
from .base import ProviderSpec, TurnCancelled

# Wall-clock guard on a single model HTTP call — the backstop that turns a
# wedged upstream into a ~5-min failure.
_CLIENT_TIMEOUT = httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=10.0)

_clients: dict[str, AsyncAnthropic] = {}


def _client(key: str, spec: ProviderSpec) -> AsyncAnthropic:
    client = _clients.get(key)
    if client is None:
        kwargs: dict = {"timeout": _CLIENT_TIMEOUT}
        if api_key := os.environ.get(spec.api_key_env):
            kwargs["api_key"] = api_key
        if spec.base_url:
            kwargs["base_url"] = spec.base_url
        client = AsyncAnthropic(**kwargs)
        _clients[key] = client
    return client


def _prune_unresolved_tool_uses(messages: list[dict]) -> None:
    while messages:
        last = messages[-1]
        if last.get("role") != "assistant":
            return
        blocks = last.get("content") or []
        if not any(
            isinstance(b, dict) and b.get("type") == "tool_use" for b in blocks
        ):
            return
        messages.pop()


def _with_cache_control(messages: list[dict]) -> list[dict]:
    """Mark the tail block so the prompt cache covers everything up to here.
    Shallow-copies; the on-disk history stays plain."""
    if not messages:
        return messages
    out = list(messages)
    last = dict(out[-1])
    content = last.get("content")
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
    base_dir = home or Path.home()
    messages: list[dict] = list(history) if history else []
    messages.append(
        {"role": "user", "content": expand_image_refs(user, base_dir=base_dir)}
    )
    try:
        return await _loop(client, spec, model, system, messages, state_dir, base_dir, drain)
    except asyncio.CancelledError:
        _prune_unresolved_tool_uses(messages)
        raise TurnCancelled(messages)


async def _loop(
    client,
    spec: ProviderSpec,
    model: str,
    system: str,
    messages: list[dict],
    state_dir: Optional[Path],
    base_dir: Path,
    drain: Optional[Callable[[], list[str]]],
) -> tuple[str, list[dict]]:
    def _drain() -> list[str]:
        return drain() if drain else []

    system_blocks = [
        {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
    ]
    for _ in range(base.MAX_ITERATIONS):
        response = await client.messages.create(
            model=model,
            max_tokens=base.MAX_TOKENS,
            system=system_blocks,
            tools=list(base.TOOL_SCHEMAS),
            messages=_with_cache_control(messages),
            extra_body=spec.extra_body,
        )
        if state_dir is not None:
            tokens.record(state_dir, model, response.usage)

        messages.append({
            "role": "assistant",
            "content": [b.model_dump() for b in response.content],
        })

        tool_uses = [b for b in response.content if b.type == "tool_use"]
        if not tool_uses:
            text_parts = [b.text for b in response.content if b.type == "text"]
            reply = "\n".join(text_parts).strip()
            # New input landed mid-generation: the turn continues with it;
            # the would-be reply is narrated so it isn't swallowed.
            if pending := _drain():
                if reply and not base.is_noop_text(reply):
                    base.narrate(reply)
                messages.append({
                    "role": "user",
                    "content": [{"type": "text", "text": t} for t in pending],
                })
                continue
            if base.is_noop_text(reply):
                return "", messages
            return reply, messages

        noop_only = all(use.name == base.NOOP_NAME for use in tool_uses)
        if noop_only:
            # Filler text alongside do_nothing stays out of narration and
            # history.
            messages[-1]["content"] = [
                block
                for block in messages[-1].get("content", [])
                if isinstance(block, dict) and block.get("type") == "tool_use"
            ]
        else:
            for block in response.content:
                if block.type == "text" and (text := (block.text or "").strip()):
                    base.narrate(text)

        tool_results = []
        for use in tool_uses:
            content, is_error = await base.run_tool(use.name, use.input)
            # Native image blocks for tools whose output can cite files.
            if use.name in base.IMAGE_TOOLS and not is_error:
                content = expand_image_refs(content, base_dir=base_dir)
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": use.id,
                "content": content,
                **({"is_error": True} if is_error else {}),
            })

        messages.append({"role": "user", "content": tool_results})
        pending = _drain()
        for t in pending:
            messages[-1]["content"].append({"type": "text", "text": t})
        if noop_only:
            if pending:
                # The model stood down, but new input just landed — handle
                # it now, not next wake.
                continue
            messages.append({
                "role": "assistant",
                "content": [{"type": "text", "text": base.NOOP_NAME}],
            })
            return "", messages

    return "[max iterations reached]", messages
