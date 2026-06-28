"""A single tool call must not be able to blow the model's context window.

Regression for the "nudge failed: maximum context length ... requested
3349548 tokens" fatal: a bash/shell command that dumps megabytes was
appended to `messages` verbatim, so the next `messages.create` in the loop
400'd. Because the bloat is regenerated inside the turn, the history-reset
overflow recovery couldn't help — the retry reproduced it and died fatally,
reaping subagents. The kernel now caps the model-bound copy of a tool result.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from boot import _shell_common
from boot import llm as L


class _Block:
    def __init__(self, type_: str, **kwargs) -> None:
        self.type = type_
        self.__dict__.update(kwargs)

    def model_dump(self) -> dict:
        out = {"type": self.type}
        if self.type == "text":
            out["text"] = self.text
        elif self.type == "tool_use":
            out.update({"id": self.id, "name": self.name, "input": self.input})
        return out


class _Messages:
    def __init__(self, responses: list[object]) -> None:
        self.responses = responses
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


def _response(*blocks: _Block):
    return SimpleNamespace(content=list(blocks), usage=SimpleNamespace())


def test_cap_tool_result_passes_small_through() -> None:
    assert L._cap_tool_result("hello") == "hello"


def test_cap_tool_result_truncates_large_keeping_head_and_tail() -> None:
    text = "A" * 1000 + "B" * 1000
    big = text * 1000  # 2_000_000 chars
    capped = L._cap_tool_result(big)

    assert len(capped) < len(big)
    assert len(capped) <= L.MAX_TOOL_RESULT_CHARS + 500  # cap + marker overhead
    assert "elided" in capped
    assert capped.startswith("A")  # head preserved
    assert capped.endswith("B")    # tail preserved


def test_loop_caps_huge_bash_output_before_sending_to_model(monkeypatch) -> None:
    # First turn: model runs one bash command. Second turn: plain reply, no tools.
    responses = [
        _response(_Block("tool_use", id="b-1", name="bash", input={"command": "x"})),
        _response(_Block("text", text="done")),
    ]
    messages_api = _Messages(responses)
    client = SimpleNamespace(messages=messages_api)

    huge = "Z" * (L.MAX_TOOL_RESULT_CHARS * 4)

    async def _fake_run(_input, env=None):
        return _shell_common.ShellResult(stdout=huge, stderr="", exit_code=0)

    monkeypatch.setattr(L.bash_tool, "run", _fake_run)
    monkeypatch.setattr(L.tokens, "record", lambda *a, **k: None)

    reply, messages = asyncio.run(
        L._loop(
            client,
            "test-model",
            {},
            "system",
            [{"role": "user", "content": "x"}],
            None,
        )
    )

    assert reply == "done"
    # The second create call carries the tool_result; its content must be capped.
    second_call_messages = messages_api.calls[1]["messages"]
    tool_result = second_call_messages[-1]["content"][0]
    assert tool_result["type"] == "tool_result"
    sent = tool_result["content"]
    assert isinstance(sent, str)
    assert len(sent) <= L.MAX_TOOL_RESULT_CHARS + 500
    assert "elided" in sent
