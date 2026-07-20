"""Anthropic-wire tool loop — one turn against the model.

Direct SDK only: every provider here speaks the Anthropic Messages wire
natively. There is no proxy process and no proxied provider (v3's
litellm path died with the kernel); OpenAI-wire providers return if and
when a direct translation layer is worth writing.

The loop runs until the model stops calling tools. `drain` is the
mid-turn injection seam: called at every tool boundary, it returns
rendered messages that arrived while the turn was running, which join
the conversation as extra user text — new input reaches a busy agent
within one model/tool step instead of waiting out the turn.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import httpx
from anthropic import AsyncAnthropic

from . import tokens, truncate
from .image_refs import expand_image_refs
from .tools import bash, edit, noop, read, shell, write

MAX_TOKENS = 4096
MAX_ITERATIONS = 200

# Cap on a single tool result fed back to the model. A command that dumps
# megabytes would otherwise inflate the prompt past the provider's context
# window mid-turn — unrecoverable by the history-reset overflow path, since
# the retry reproduces the same bloat. The full output still reaches
# stdout/journald; only the model-bound copy is truncated. ~50K tokens.
MAX_TOOL_RESULT_CHARS = 200_000

# Wall-clock guard on a single model HTTP call — the backstop that turns a
# wedged upstream into a ~5-min failure.
_CLIENT_TIMEOUT = httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=10.0)


def _cap_tool_result(text: str) -> str:
    if len(text) <= MAX_TOOL_RESULT_CHARS:
        return text
    half = MAX_TOOL_RESULT_CHARS // 2
    elided = len(text) - 2 * half
    marker = (
        f"\n\n[... {elided} characters elided — tool output exceeded the "
        f"{MAX_TOOL_RESULT_CHARS}-char cap; the full output is in the log. "
        f"Re-run narrowed (head/tail/grep/sed -n) to see more ...]\n\n"
    )
    return text[:half] + marker + text[-half:]


@dataclass(frozen=True)
class ProviderSpec:
    base_url: Optional[str]
    api_key_env: str
    default_model: str
    extra_body: dict = field(default_factory=dict)


PROVIDERS: dict[str, ProviderSpec] = {
    "anthropic": ProviderSpec(None, "ANTHROPIC_API_KEY", "claude-sonnet-4-6"),
    # Anthropic-compatible endpoints, reached directly.
    "deepseek": ProviderSpec(
        "https://api.deepseek.com/anthropic", "DEEPSEEK_API_KEY", "deepseek-v4-pro"
    ),
    "zai": ProviderSpec("https://api.z.ai/api/anthropic", "ZAI_API_KEY", "glm-5.2"),
}
DEFAULT_PROVIDER = "anthropic"

_clients: dict[str, AsyncAnthropic] = {}


def _resolve(provider: Optional[str], model: Optional[str]) -> tuple[AsyncAnthropic, str, dict]:
    key = provider or DEFAULT_PROVIDER
    if key not in PROVIDERS:
        raise ValueError(f"unknown provider: {key!r}")
    spec = PROVIDERS[key]
    # Strip a self-referential prefix ("anthropic/claude-…"); any other
    # slash is part of the model id.
    if model and "/" in model:
        head, rest = model.split("/", 1)
        if head == key:
            model = rest
    client = _clients.get(key)
    if client is None:
        kwargs: dict = {"timeout": _CLIENT_TIMEOUT}
        if api_key := os.environ.get(spec.api_key_env):
            kwargs["api_key"] = api_key
        if spec.base_url:
            kwargs["base_url"] = spec.base_url
        client = AsyncAnthropic(**kwargs)
        _clients[key] = client
    return client, model or spec.default_model, spec.extra_body


class TurnCancelled(Exception):
    """Cancelled mid-loop; carries the partial history, pruned of any
    trailing assistant turn with unresolved tool_uses, for persist+resume."""

    def __init__(self, messages: list[dict]):
        super().__init__("turn cancelled")
        self.messages = messages


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


def _narrate(text: str) -> None:
    for line in text.splitlines():
        if line := line.strip():
            print(f"» {line}", flush=True)


async def run_turn(
    system: str,
    user: str,
    history: Optional[list[dict]] = None,
    *,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    state_dir: Optional[Path] = None,
    home: Optional[Path] = None,
    drain: Optional[Callable[[], list[str]]] = None,
) -> tuple[str, list[dict]]:
    """Run one turn. Returns (final assistant text, full message list =
    history + this turn's messages); the caller persists it. Raises
    TurnCancelled with pruned partial history on cancellation."""
    client, model, extra_body = _resolve(provider, model)
    base_dir = home or Path.home()
    messages: list[dict] = list(history) if history else []
    messages.append({"role": "user", "content": expand_image_refs(user, base_dir=base_dir)})
    try:
        return await _loop(
            client, model, extra_body, system, messages, state_dir, base_dir, drain
        )
    except asyncio.CancelledError:
        _prune_unresolved_tool_uses(messages)
        raise TurnCancelled(messages)


async def _loop(
    client: AsyncAnthropic,
    model: str,
    extra_body: dict,
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
    for _ in range(MAX_ITERATIONS):
        response = await client.messages.create(
            model=model,
            max_tokens=MAX_TOKENS,
            system=system_blocks,
            tools=[
                bash.TOOL_SCHEMA,
                shell.TOOL_SCHEMA,
                read.TOOL_SCHEMA,
                edit.TOOL_SCHEMA,
                write.TOOL_SCHEMA,
                noop.TOOL_SCHEMA,
            ],
            messages=_with_cache_control(messages),
            extra_body=extra_body,
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
            # New input landed mid-generation: the turn continues with it
            # instead of ending; the would-be reply is narrated so it isn't
            # swallowed by the continuation.
            if pending := _drain():
                if reply and not noop.is_sentinel_text(reply):
                    _narrate(reply)
                messages.append({
                    "role": "user",
                    "content": [{"type": "text", "text": t} for t in pending],
                })
                continue
            # A quiet turn expressed as prose instead of the do_nothing tool.
            if noop.is_sentinel_text(reply):
                return "", messages
            return reply, messages

        noop_only = all(use.name == noop.TOOL_NAME for use in tool_uses)
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
                    _narrate(text)

        tool_results = []
        for use in tool_uses:
            if use.name == bash.TOOL_NAME:
                print(f"$ {use.input.get('command', '')}", flush=True)
                result = await bash.run(use.input)
                rendered = result.render()
                print(rendered, flush=True)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": use.id,
                    "content": expand_image_refs(
                        _cap_tool_result(
                            truncate.cap_tail_for_model(rendered, tool=use.name)
                        ),
                        base_dir=base_dir,
                    ),
                })
            elif use.name == shell.TOOL_NAME:
                if use.input.get("keys"):
                    print(f"[keys] {use.input['keys']}", flush=True)
                else:
                    print(f"$ {use.input.get('command', '')}", flush=True)
                result = await shell.run(use.input)
                rendered = result.render()
                print(rendered, flush=True)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": use.id,
                    "content": expand_image_refs(
                        _cap_tool_result(
                            truncate.cap_tail_for_model(rendered, tool=use.name)
                        ),
                        base_dir=base_dir,
                    ),
                })
            elif use.name in (read.TOOL_NAME, edit.TOOL_NAME, write.TOOL_NAME):
                mod = {read.TOOL_NAME: read, edit.TOOL_NAME: edit, write.TOOL_NAME: write}[use.name]
                print(f"{use.name} {use.input.get('path', '')}", flush=True)
                result = mod.run(use.input)
                print(result.text, flush=True)
                content = _cap_tool_result(result.text)
                if mod is read:
                    content = expand_image_refs(content, base_dir=base_dir)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": use.id,
                    "content": content,
                    **({"is_error": True} if result.is_error else {}),
                })
            elif use.name == noop.TOOL_NAME:
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": use.id,
                    "content": noop.TOOL_RESULT,
                })
            else:
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": use.id,
                    "content": f"unknown tool: {use.name}",
                    "is_error": True,
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
                "content": [{"type": "text", "text": noop.TOOL_NAME}],
            })
            return "", messages

    return "[max iterations reached]", messages
