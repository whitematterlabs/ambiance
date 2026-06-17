"""Auto-compaction at the nudge chokepoint.

When a PAI's last_window_tokens crosses its configured threshold, the
next nudge to it is preceded by a kernel-issued `kernel:compact` nudge.
Concurrent nudges queue behind the compaction and drain in order.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from boot import nudge as N
from boot import processes as P


def _spawn(slug: str, *, pid: int, **extra) -> None:
    P.spawn_pai(pid=pid, slug=slug, description=f"{slug} test", extra=extra or None)


def _write_tokens(slug: str, last_window: int) -> None:
    (P.PROC_DIR / slug / "tokens").write_text(
        json.dumps({"last_window_tokens": last_window})
    )


def _write_history(slug: str, n_messages: int) -> Path:
    path = P.HOME_DIR / "proc" / slug / "messages.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for i in range(n_messages):
            role = "user" if i % 2 == 0 else "assistant"
            f.write(json.dumps({"role": role, "content": f"msg{i}"}) + "\n")
    return path


@pytest.fixture(autouse=True)
def _reset_compaction_state(live_dir: Path, monkeypatch: pytest.MonkeyPatch):
    # Clear the module-level lock map and cooldown between tests so they
    # don't bleed state.
    monkeypatch.setattr(N, "_pai_locks", {}, raising=True)
    monkeypatch.setattr(N, "_recently_compacted", {}, raising=True)
    monkeypatch.setattr(N, "_TRANSIENT_RETRY_DELAY", 0, raising=True)  # no real sleep
    # nudge.py imports HOME_DIR by name at module load — re-bind it so
    # _apply_history_action looks at the test tree, not the real ~/.pai.
    monkeypatch.setattr(N, "HOME_DIR", P.HOME_DIR, raising=True)
    monkeypatch.setattr(N, "PROC_DIR", P.PROC_DIR, raising=True)


def test_threshold_triggers_compact_then_original_nudge(live_dir: Path) -> None:
    _spawn("alpha", pid=10, compact_threshold=1000)
    _write_tokens("alpha", 5000)
    history_path = _write_history("alpha", 8)
    proc_dir = P.HOME_DIR / "proc" / "alpha"

    calls: list[str] = []

    async def fake_run_turn(system, user, history=None, env=None, *, provider=None, model=None, set_status=None):
        # The first call is the compact nudge — simulate the PAI calling
        # bin/compact during the turn by writing the .history-action file.
        if "kernel:compact" in user:
            calls.append("compact")
            (proc_dir / ".history-action").write_text("compact\nDistilled summary.\n")
            return ("compacted", list(history or []) + [
                {"role": "user", "content": user},
                {"role": "assistant", "content": "compacted"},
            ])
        calls.append("original")
        return ("ok", list(history or []) + [
            {"role": "user", "content": user},
            {"role": "assistant", "content": "ok"},
        ])

    import boot.llm as L
    L_orig = L.run_turn
    L.run_turn = fake_run_turn  # type: ignore[assignment]
    try:
        asyncio.run(N.nudge(reason="hello", to=10, from_kind="kernel"))
    finally:
        L.run_turn = L_orig  # type: ignore[assignment]

    assert calls == ["compact", "original"]
    # History was archived.
    archives = list((proc_dir / "history").glob("*-compact.jsonl"))
    assert len(archives) == 1
    # Live history is now the 2-message stub.
    live = [json.loads(ln) for ln in history_path.read_text().splitlines() if ln.strip()]
    # After original nudge, the stub (2) plus original turn (2) = 4.
    # The compaction stub itself is the first two entries.
    assert live[0]["content"].startswith("[compacted prior context]")
    assert "Distilled summary." in live[0]["content"]


def test_no_compact_when_under_threshold(live_dir: Path) -> None:
    _spawn("beta", pid=11, compact_threshold=10000)
    _write_tokens("beta", 500)
    _write_history("beta", 2)

    calls: list[str] = []

    async def fake_run_turn(system, user, history=None, env=None, *, provider=None, model=None, set_status=None):
        calls.append("compact" if "kernel:compact" in user else "original")
        return ("ok", list(history or []) + [
            {"role": "user", "content": user},
            {"role": "assistant", "content": "ok"},
        ])

    import boot.llm as L
    L_orig = L.run_turn
    L.run_turn = fake_run_turn  # type: ignore[assignment]
    try:
        asyncio.run(N.nudge(reason="hello", to=11, from_kind="kernel"))
    finally:
        L.run_turn = L_orig  # type: ignore[assignment]

    assert calls == ["original"]


def test_no_compact_when_no_tokens_file(live_dir: Path) -> None:
    # First-turn case: /proc/<slug>/tokens doesn't exist yet.
    _spawn("gamma", pid=12, compact_threshold=1)
    _write_history("gamma", 0)

    calls: list[str] = []

    async def fake_run_turn(system, user, history=None, env=None, *, provider=None, model=None, set_status=None):
        calls.append("compact" if "kernel:compact" in user else "original")
        return ("ok", [{"role": "user", "content": user},
                       {"role": "assistant", "content": "ok"}])

    import boot.llm as L
    L_orig = L.run_turn
    L.run_turn = fake_run_turn  # type: ignore[assignment]
    try:
        asyncio.run(N.nudge(reason="hello", to=12, from_kind="kernel"))
    finally:
        L.run_turn = L_orig  # type: ignore[assignment]

    assert calls == ["original"]


def test_concurrent_nudges_queue_behind_compaction(live_dir: Path) -> None:
    _spawn("delta", pid=13, compact_threshold=1000)
    _write_tokens("delta", 5000)
    _write_history("delta", 4)
    proc_dir = P.HOME_DIR / "proc" / "delta"

    order: list[str] = []
    compact_started = asyncio.Event()
    release_compact = asyncio.Event()

    async def fake_run_turn(system, user, history=None, env=None, *, provider=None, model=None, set_status=None):
        if "kernel:compact" in user:
            order.append("compact-start")
            compact_started.set()
            await release_compact.wait()
            (proc_dir / ".history-action").write_text("compact\nSummary.\n")
            order.append("compact-end")
        else:
            # Tag with reason so we can verify FIFO drain.
            tag = "first" if "first" in user else "second"
            order.append(tag)
        return ("ok", list(history or []) + [
            {"role": "user", "content": user},
            {"role": "assistant", "content": "ok"},
        ])

    import boot.llm as L
    L_orig = L.run_turn
    L.run_turn = fake_run_turn  # type: ignore[assignment]

    async def runner():
        t1 = asyncio.create_task(N.nudge(reason="first", to=13, from_kind="kernel"))
        # Wait until compaction is in flight, THEN fire the second nudge so
        # it queues behind the lock that t1 is holding.
        await compact_started.wait()
        t2 = asyncio.create_task(N.nudge(reason="second", to=13, from_kind="kernel"))
        # Give the second task a chance to reach the lock.
        await asyncio.sleep(0.05)
        release_compact.set()
        await asyncio.gather(t1, t2)

    try:
        asyncio.run(runner())
    finally:
        L.run_turn = L_orig  # type: ignore[assignment]

    # Compaction completes before either original nudge runs; first
    # finishes before second (FIFO).
    assert order[0] == "compact-start"
    assert order[1] == "compact-end"
    # The cooldown prevents a second compaction for the queued nudge.
    assert order[2:] == ["first", "second"]


# --- Hard-limit recovery: when the model never compacted and history blew
# --- past the provider's context window, the kernel resets it itself.


def _patch_run_turn(fake):
    import boot.llm as L
    orig = L.run_turn
    L.run_turn = fake  # type: ignore[assignment]
    return L, orig


def test_context_overflow_archives_resets_and_retries(live_dir: Path) -> None:
    _spawn("omega", pid=30)
    history_path = _write_history("omega", 6)
    proc_dir = P.HOME_DIR / "proc" / "omega"

    seen_history_lens: list[int] = []

    async def fake_run_turn(system, user, history=None, env=None, *, provider=None, model=None, set_status=None):
        seen_history_lens.append(len(history or []))
        if len(seen_history_lens) == 1:
            # Provider rejects the oversized prompt (Anthropic/OpenAI shape).
            raise RuntimeError(
                "Error code: 400 - this model's maximum context length is "
                "1048565 tokens. However, you requested 1051452 tokens."
            )
        return ("recovered", list(history or []) + [
            {"role": "user", "content": user},
            {"role": "assistant", "content": "recovered"},
        ])

    L, orig = _patch_run_turn(fake_run_turn)
    try:
        asyncio.run(N.nudge(reason="hello", to=30, from_kind="kernel"))
    finally:
        L.run_turn = orig  # type: ignore[assignment]

    # First attempt saw the full history; retry saw a reset (empty) history.
    assert seen_history_lens == [6, 0]
    # The oversized history was archived under an -overflow tag.
    archives = list((proc_dir / "history").glob("*-overflow.jsonl"))
    assert len(archives) == 1
    archived = [json.loads(ln) for ln in archives[0].read_text().splitlines() if ln.strip()]
    assert len(archived) == 6
    # Live history is the recovered turn only (reset + the successful retry).
    live = [json.loads(ln) for ln in history_path.read_text().splitlines() if ln.strip()]
    assert len(live) == 2
    assert live[-1]["content"] == "recovered"


def test_overflow_retry_failure_does_not_escalate_to_root(live_dir: Path) -> None:
    # If even the reset retry fails, the failure must NOT be escalated to root
    # (overflow is transient/self-handled — escalating snowballs into a storm).
    _spawn("root", pid=1)
    _spawn("worker", pid=31)
    _write_history("worker", 4)

    seen_pids: list[str] = []

    async def fake_run_turn(system, user, history=None, env=None, *, provider=None, model=None, set_status=None):
        seen_pids.append((env or {}).get("PAI_PID"))
        raise RuntimeError("maximum context length exceeded")

    L, orig = _patch_run_turn(fake_run_turn)
    try:
        asyncio.run(N.nudge(reason="hello", to=31, from_kind="kernel"))
    finally:
        L.run_turn = orig  # type: ignore[assignment]

    # Worker tried twice (initial + reset retry); root was never nudged.
    assert seen_pids == ["31", "31"]
    assert "1" not in seen_pids


def test_transient_error_not_escalated_to_root(live_dir: Path) -> None:
    _spawn("root", pid=1)
    _spawn("worker", pid=32)
    _write_history("worker", 2)

    seen_pids: list[str] = []

    async def fake_run_turn(system, user, history=None, env=None, *, provider=None, model=None, set_status=None):
        pid = (env or {}).get("PAI_PID")
        seen_pids.append(pid)
        if pid == "32":
            raise RuntimeError("Connection error.")
        return ("ok", [{"role": "assistant", "content": "ok"}])

    L, orig = _patch_run_turn(fake_run_turn)
    try:
        asyncio.run(N.nudge(reason="hello", to=32, from_kind="kernel"))
    finally:
        L.run_turn = orig  # type: ignore[assignment]

    # A network blip is logged and dropped — root is never woken.
    # The kernel retries once (two attempts total), then gives up silently.
    assert seen_pids == ["32", "32"]
    assert "1" not in seen_pids


def test_genuine_error_still_escalates_to_root(live_dir: Path) -> None:
    _spawn("root", pid=1)
    _spawn("worker", pid=33)
    _write_history("worker", 2)

    seen_pids: list[str] = []

    async def fake_run_turn(system, user, history=None, env=None, *, provider=None, model=None, set_status=None):
        pid = (env or {}).get("PAI_PID")
        seen_pids.append(pid)
        if pid == "33":
            raise RuntimeError("KeyError: 'unexpected_field'")
        return ("ok", [{"role": "assistant", "content": "ok"}])

    L, orig = _patch_run_turn(fake_run_turn)
    try:
        asyncio.run(N.nudge(reason="hello", to=33, from_kind="kernel"))
    finally:
        L.run_turn = orig  # type: ignore[assignment]

    # A genuine, actionable bug IS surfaced to root (pid 1 gets nudged).
    assert "33" in seen_pids
    assert "1" in seen_pids
