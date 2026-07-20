"""Shared backend substrate — everything that is NOT wire-specific.

A backend module owns one provider wire format end to end: request
shape, tool-schema rendering, history message shapes, prompt-cache
idiom, usage extraction. What lives here is the common ground both
wires stand on: the provider registry, tool execution, result capping,
and the cancellation contract. There is deliberately no translation
layer between wires — that was litellm's job and half its bugs; a
conversation lives and persists in its backend's native shape.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .. import truncate
from ..tools import bash, edit, noop, read, shell, write

MAX_TOKENS = 4096
MAX_ITERATIONS = 200

# Cap on a single tool result fed back to the model. A command that dumps
# megabytes would otherwise inflate the prompt past the provider's context
# window mid-turn — unrecoverable by the history-reset overflow path, since
# the retry reproduces the same bloat. The full output still reaches
# stdout/journald; only the model-bound copy is truncated. ~50K tokens.
MAX_TOOL_RESULT_CHARS = 200_000


@dataclass(frozen=True)
class ProviderSpec:
    wire: str  # "anthropic" | "openai" — selects the backend module
    base_url: Optional[str]
    api_key_env: str
    default_model: str
    extra_body: dict = field(default_factory=dict)


PROVIDERS: dict[str, ProviderSpec] = {
    # Anthropic wire, reached directly.
    "anthropic": ProviderSpec("anthropic", None, "ANTHROPIC_API_KEY", "claude-sonnet-4-6"),
    "deepseek": ProviderSpec(
        "anthropic", "https://api.deepseek.com/anthropic", "DEEPSEEK_API_KEY", "deepseek-v4-pro"
    ),
    "zai": ProviderSpec(
        "anthropic", "https://api.z.ai/api/anthropic", "ZAI_API_KEY", "glm-5.2"
    ),
    # OpenAI wire, reached directly — no proxy in v4. OpenRouter is
    # OpenAI-wire native, so dropping litellm un-deferred it.
    "openai": ProviderSpec("openai", None, "OPENAI_API_KEY", "gpt-5.5"),
    "openrouter": ProviderSpec(
        "openai",
        "https://openrouter.ai/api/v1",
        "OPENROUTER_API_KEY",
        "nvidia/nemotron-3-ultra-550b-a55b:free",
    ),
}
DEFAULT_PROVIDER = "anthropic"


def resolve(provider: Optional[str], model: Optional[str]) -> tuple[str, ProviderSpec, str]:
    """(provider key, spec, model) — with the self-referential model prefix
    ("anthropic/claude-…") stripped; any other slash is part of the id
    (OpenRouter slugs are vendor/model)."""
    key = provider or DEFAULT_PROVIDER
    if key not in PROVIDERS:
        raise ValueError(f"unknown provider: {key!r}")
    spec = PROVIDERS[key]
    if model and "/" in model:
        head, rest = model.split("/", 1)
        if head == key:
            model = rest
    return key, spec, model or spec.default_model


def wire_for(provider: Optional[str]) -> str:
    return resolve(provider, None)[1].wire


class TurnCancelled(Exception):
    """Cancelled mid-loop; carries the partial history, pruned by the
    backend of any trailing assistant turn with unresolved tool calls,
    for persist+resume."""

    def __init__(self, messages: list[dict]):
        super().__init__("turn cancelled")
        self.messages = messages


def cap_result(text: str) -> str:
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


def narrate(text: str) -> None:
    for line in text.splitlines():
        if line := line.strip():
            print(f"» {line}", flush=True)


TOOL_SCHEMAS = (
    bash.TOOL_SCHEMA,
    shell.TOOL_SCHEMA,
    read.TOOL_SCHEMA,
    edit.TOOL_SCHEMA,
    write.TOOL_SCHEMA,
    noop.TOOL_SCHEMA,
)

# Tools whose output can cite image files worth inlining.
IMAGE_TOOLS = frozenset({bash.TOOL_NAME, shell.TOOL_NAME, read.TOOL_NAME})

NOOP_NAME = noop.TOOL_NAME
NOOP_RESULT = noop.TOOL_RESULT
is_noop_text = noop.is_sentinel_text


async def run_tool(name: str, tool_input: dict) -> tuple[str, bool]:
    """Execute one tool call; returns (model-bound text, is_error). The
    model-bound copy is tail-capped + hard-capped; the full output goes to
    stdout/journald. Wire packaging (blocks vs role:tool) is the caller's."""
    if name == bash.TOOL_NAME:
        print(f"$ {tool_input.get('command', '')}", flush=True)
        result = await bash.run(tool_input)
        rendered = result.render()
        print(rendered, flush=True)
        return cap_result(truncate.cap_tail_for_model(rendered, tool=name)), False
    if name == shell.TOOL_NAME:
        if tool_input.get("keys"):
            print(f"[keys] {tool_input['keys']}", flush=True)
        else:
            print(f"$ {tool_input.get('command', '')}", flush=True)
        result = await shell.run(tool_input)
        rendered = result.render()
        print(rendered, flush=True)
        return cap_result(truncate.cap_tail_for_model(rendered, tool=name)), False
    if name in (read.TOOL_NAME, edit.TOOL_NAME, write.TOOL_NAME):
        mod = {read.TOOL_NAME: read, edit.TOOL_NAME: edit, write.TOOL_NAME: write}[name]
        print(f"{name} {tool_input.get('path', '')}", flush=True)
        result = mod.run(tool_input)
        print(result.text, flush=True)
        return cap_result(result.text), bool(result.is_error)
    if name == NOOP_NAME:
        return NOOP_RESULT, False
    return f"unknown tool: {name}", True
