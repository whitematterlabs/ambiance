"""Tests for the terminal NOOP tool."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from boot import llm as L
from boot import noop_tool


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


def test_noop_schema_is_registered(monkeypatch) -> None:
    response = _response(_Block("tool_use", id="noop-1", name="NOOP", input={}))
    messages_api = _Messages([response])
    client = SimpleNamespace(messages=messages_api)
    monkeypatch.setattr(L.tokens, "record", lambda *args, **kwargs: None)

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

    assert reply == ""
    assert messages_api.calls[0]["tools"][-1] == noop_tool.TOOL_SCHEMA
    assert len(messages_api.calls) == 1
    assert messages[-2] == {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": "noop-1",
                "content": noop_tool.TOOL_RESULT,
            }
        ],
    }
    assert messages[-1] == {
        "role": "assistant",
        "content": [{"type": "text", "text": noop_tool.TOOL_NAME}],
    }


def test_noop_only_suppresses_interim_narration(monkeypatch) -> None:
    response = _response(
        _Block("text", text="Quiet."),
        _Block("tool_use", id="noop-1", name="NOOP", input={}),
    )
    client = SimpleNamespace(messages=_Messages([response]))
    log_lines: list[str] = []
    thread_lines: list[str] = []
    monkeypatch.setattr(L.tokens, "record", lambda *args, **kwargs: None)
    monkeypatch.setattr(L, "append_log", lambda slug, line: log_lines.append(line))
    monkeypatch.setattr(
        L,
        "_append_narration_to_me_thread",
        lambda pai_pid, line: thread_lines.append(line),
    )

    reply, messages = asyncio.run(
        L._loop(
            client,
            "test-model",
            {},
            "system",
            [{"role": "user", "content": "x"}],
            {"PAI_SLUG": "alpha", "PAI_PID": "7"},
        )
    )

    assert reply == ""
    assert log_lines == []
    assert thread_lines == []
    assert messages[-3] == {
        "role": "assistant",
        "content": [
            {"type": "tool_use", "id": "noop-1", "name": "NOOP", "input": {}}
        ],
    }
