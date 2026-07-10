"""Mid-turn message injection (boot.inject).

A message sent to a PAI whose turn is running must not wait behind the
slug lock for the whole turn (for subagents that meant racing the reap
and dropping). It is queued via inject.try_inject, drained by llm._loop
at the next tool boundary, and fed into the live conversation; anything
still queued when the turn ends is re-emitted onto the nudge path.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from boot import inject as I
from boot import llm as L
from boot import noop_tool
from boot import nudge as N
from boot import processes as P


@pytest.fixture(autouse=True)
def _reset(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(I, "_live", {}, raising=True)
    monkeypatch.setattr(N, "_pai_locks", {}, raising=True)


# ---------------------------------------------------------------- registry


def test_try_inject_requires_live_turn() -> None:
    assert I.try_inject("ghost", "peer message", context={"text": "hi"}) is False

    assert I.register_turn("alpha") is True
    assert I.register_turn("alpha") is False  # already open — shared window
    assert I.try_inject("alpha", "peer message", context={"text": "hi"}) is True

    assert I.drain("alpha") != []
    assert I.drain("alpha") == []  # drained exactly once

    I.end_turn("alpha")
    assert I.try_inject("alpha", "peer message", context={"text": "hi"}) is False


def test_end_turn_returns_undrained_events_for_reemit() -> None:
    I.register_turn("beta")
    ev = {"kind": "pai_message", "target_pid": 7, "text": "late"}
    assert I.try_inject("beta", "peer message", context={"text": "late"}, event=ev)
    assert I.end_turn("beta") == [ev]
    # Window closed: nothing left to drain, nothing to re-emit twice.
    assert I.drain("beta") == []
    assert I.end_turn("beta") == []


def test_overclock_context_is_never_injected() -> None:
    I.register_turn("gamma")
    ok = I.try_inject(
        "gamma", "owner message", context={"text": "go", "overclock": True}
    )
    assert ok is False


def test_drain_without_slug_or_window_is_empty() -> None:
    assert I.drain(None) == []
    assert I.drain("nobody") == []


# ---------------------------------------------------------------- llm loop


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


def _run_loop(responses: list[object], env: dict) -> tuple[str, list[dict]]:
    client = SimpleNamespace(messages=_Messages(responses))
    return asyncio.run(
        L._loop(client, "test-model", {}, "system",
                [{"role": "user", "content": "x"}], env)
    )


def test_injection_lands_at_tool_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    """A message queued while tools run rides the tool_result user turn and
    the turn continues — even when the model had stood down."""
    monkeypatch.setattr(L.tokens, "record", lambda *a, **k: None)
    I.register_turn("t1")
    assert I.try_inject("t1", "peer message", context={"text": "correction!"})

    responses = [
        _response(_Block("tool_use", id="n1", name=noop_tool.TOOL_NAME, input={})),
        _response(_Block("text", text="handled the correction")),
    ]
    reply, messages = _run_loop(responses, env={"PAI_SLUG": "t1"})

    assert reply == "handled the correction"
    # The do_nothing result and the injected message share one user turn.
    boundary = messages[-2]
    assert boundary["role"] == "user"
    types = [b["type"] for b in boundary["content"]]
    assert types == ["tool_result", "text"]
    assert "correction!" in boundary["content"][1]["text"]
    assert I.drain("t1") == []


def test_injection_preempts_final_reply(
    live_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A message pending when the model produces a final reply continues the
    turn: the message becomes the next user input, the interim reply is
    narrated, and the loop's next answer is the turn's reply."""
    monkeypatch.setattr(L.tokens, "record", lambda *a, **k: None)
    narrated: list[str] = []
    monkeypatch.setattr(L, "_narrate", lambda slug, text: narrated.append(text))
    I.register_turn("t2")
    assert I.try_inject("t2", "owner message", context={"text": "one more thing"})

    responses = [
        _response(_Block("text", text="all done")),
        _response(_Block("text", text="also did the extra thing")),
    ]
    reply, messages = _run_loop(responses, env={"PAI_SLUG": "t2"})

    assert reply == "also did the extra thing"
    assert narrated == ["all done"]
    injected_turn = messages[-2]
    assert injected_turn["role"] == "user"
    assert "one more thing" in injected_turn["content"][0]["text"]


# -------------------------------------------------------------- nudge glue


def _spawn(slug: str, *, pid: int, **extra) -> None:
    P.spawn_pai(pid=pid, slug=slug, description=f"{slug} test", extra=extra or None)


def _run_nudge(fake, *, to: int, reason: str = "hello"):
    orig = L.run_turn
    L.run_turn = fake  # type: ignore[assignment]
    try:
        asyncio.run(N.nudge(reason=reason, to=to, from_kind="kernel"))
    finally:
        L.run_turn = orig  # type: ignore[assignment]


@pytest.fixture(autouse=True)
def _nudge_paths(live_dir: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(N, "HOME_DIR", P.HOME_DIR, raising=True)
    monkeypatch.setattr(N, "PROC_DIR", P.PROC_DIR, raising=True)


def test_injection_window_open_during_turn(live_dir: Path) -> None:
    _spawn("zeta", pid=21)
    seen: list[bool] = []

    async def fake_run_turn(system, user, history=None, env=None, *, provider=None,
                            model=None, set_status=None):
        seen.append(I.try_inject("zeta", "peer message", context={"text": "mid"}))
        # Consume it, as llm._loop would at the next boundary.
        assert any("mid" in t for t in I.drain("zeta"))
        return ("ok", list(history or []) + [
            {"role": "user", "content": user},
            {"role": "assistant", "content": "ok"},
        ])

    _run_nudge(fake_run_turn, to=21)
    assert seen == [True]
    # Window closed after the turn.
    assert I.try_inject("zeta", "peer message", context={"text": "late"}) is False


def test_undrained_injection_reemitted_after_turn(live_dir: Path) -> None:
    _spawn("eta", pid=22)
    ev = {"kind": "pai_message", "target_pid": 22, "sender_pid": 2, "text": "late"}

    async def fake_run_turn(system, user, history=None, env=None, *, provider=None,
                            model=None, set_status=None):
        assert I.try_inject("eta", "peer message", context={"text": "late"}, event=ev)
        return ("ok", list(history or []) + [
            {"role": "user", "content": user},
            {"role": "assistant", "content": "ok"},
        ])

    _run_nudge(fake_run_turn, to=22)

    reemitted = []
    for f in P.EVENTS_DIR.glob("*"):
        data = yaml.safe_load(f.read_text())
        if isinstance(data, dict) and data.get("kind") == "pai_message":
            reemitted.append(data)
    assert any(e.get("text") == "late" for e in reemitted)


def test_compact_turn_has_no_injection_window(live_dir: Path) -> None:
    _spawn("theta", pid=23)
    seen: list[bool] = []

    async def fake_run_turn(system, user, history=None, env=None, *, provider=None,
                            model=None, set_status=None):
        seen.append(I.try_inject("theta", "peer message", context={"text": "x"}))
        return ("ok", list(history or []) + [
            {"role": "user", "content": user},
            {"role": "assistant", "content": "ok"},
        ])

    _run_nudge(fake_run_turn, to=23, reason="kernel:compact")
    assert seen == [False]
