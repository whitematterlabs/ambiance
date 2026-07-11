"""Tests for the terminal NOOP tool."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from boot import bash_tool, bootstrap, llm as L
from boot import noop_tool
from boot import shell_tool


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


def test_noop_policy_is_required_for_quiet_turns() -> None:
    instructions = " ".join(bootstrap.OPERATING_INSTRUCTIONS.split())

    assert "end by calling the `do_nothing` tool" in instructions
    assert "required for quiet turns" in instructions
    assert "never write the word `do_nothing`" in instructions
    assert "one-line reply is preferred over silence" not in instructions


def test_noop_schema_is_registered(monkeypatch) -> None:
    response = _response(
        _Block("tool_use", id="noop-1", name=noop_tool.TOOL_NAME, input={})
    )
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
    assert messages_api.calls[0]["tools"] == [
        bash_tool.TOOL_SCHEMA,
        shell_tool.TOOL_SCHEMA,
        noop_tool.TOOL_SCHEMA,
    ]
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


def test_sentinel_prose_is_canonicalized_to_no_reply(monkeypatch) -> None:
    # The model types the sentinel as text instead of calling the tool; the
    # kernel must swallow it rather than surfacing "NOOP" as a message.
    response = _response(_Block("text", text="NOOP"))
    client = SimpleNamespace(messages=_Messages([response]))
    monkeypatch.setattr(L.tokens, "record", lambda *args, **kwargs: None)

    reply, _ = asyncio.run(
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


def test_real_reply_is_not_swallowed(monkeypatch) -> None:
    response = _response(_Block("text", text="Done — profile updated."))
    client = SimpleNamespace(messages=_Messages([response]))
    monkeypatch.setattr(L.tokens, "record", lambda *args, **kwargs: None)

    reply, _ = asyncio.run(
        L._loop(
            client,
            "test-model",
            {},
            "system",
            [{"role": "user", "content": "x"}],
            None,
        )
    )

    assert reply == "Done — profile updated."


def test_noop_only_suppresses_interim_narration(monkeypatch) -> None:
    response = _response(
        _Block("text", text="Quiet."),
        _Block("tool_use", id="noop-1", name=noop_tool.TOOL_NAME, input={}),
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
            {
                "type": "tool_use",
                "id": "noop-1",
                "name": noop_tool.TOOL_NAME,
                "input": {},
            }
        ],
    }


@pytest.mark.parametrize("prose", [
    "*(no reply needed)*",
    "(no reply needed)",
    "No reply needed.",
    "`do_nothing`",
    '"quiet"',
    "*Nothing to do.*",
    "no action needed",
])
def test_wrapped_and_new_sentinel_variants_absorbed(prose) -> None:
    assert noop_tool.is_sentinel_text(prose)


@pytest.mark.parametrize("prose", [
    "No reply needed — but I archived the thread.",
    "Done — profile updated.",
    "The owner asked me to do nothing about the email backlog, so I left it.",
])
def test_substantive_replies_not_absorbed(prose) -> None:
    assert not noop_tool.is_sentinel_text(prose)
