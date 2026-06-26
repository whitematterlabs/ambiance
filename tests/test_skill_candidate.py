"""Unit tests for the post-turn skill-candidate trigger in `boot.nudge`.

Pins the predicate that decides whether a finished turn is offered to
librarian-pai as a skill candidate, and the tool-call counter that feeds it.
"""

from __future__ import annotations

from boot import nudge


def test_predicate_fires_on_long_duration() -> None:
    assert nudge._is_skill_candidate("main", duration=31, tool_calls=0)


def test_predicate_fires_on_many_tool_calls() -> None:
    assert nudge._is_skill_candidate("main", duration=1, tool_calls=6)


def test_predicate_does_not_fire_on_trivial_turn() -> None:
    assert not nudge._is_skill_candidate("main", duration=5, tool_calls=2)


def test_predicate_boundaries_are_strict() -> None:
    # Thresholds are strict `>` — exactly at the bound does not fire.
    assert not nudge._is_skill_candidate("main", duration=30, tool_calls=5)
    assert nudge._is_skill_candidate("main", duration=30.1, tool_calls=5)
    assert nudge._is_skill_candidate("main", duration=30, tool_calls=6)


def test_predicate_never_fires_for_librarian() -> None:
    # Loop guard: librarian's own turns hit the same path; it must never
    # nominate itself or it would re-wake on its own output.
    assert not nudge._is_skill_candidate("librarian-pai", duration=999, tool_calls=99)


def test_count_tool_calls_counts_tool_use_blocks() -> None:
    messages = [
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "working"},
                {"type": "tool_use", "name": "bash", "id": "1"},
                {"type": "tool_use", "name": "read", "id": "2"},
            ],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "1", "content": "ok"}],
        },
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "name": "edit", "id": "3"}],
        },
    ]
    assert nudge._count_tool_calls(messages) == 3


def test_count_tool_calls_zero_for_plain_text() -> None:
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello, no tools here"},
    ]
    assert nudge._count_tool_calls(messages) == 0
