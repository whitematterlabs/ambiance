"""Focused tests for the v4 member-plane tool suite (`agent.tools`)."""

from __future__ import annotations

import asyncio
import os
import signal
import tempfile
import time
from pathlib import Path

import pytest

from agent import truncate
from agent.tools import bash, edit, read, write


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# File tools: write → read → edit → read round-trip


def test_file_round_trip(tmp_path: Path):
    env = {"HOME": str(tmp_path)}

    w = write.run({"path": "notes/hello.txt", "content": "alpha\nbeta\n"}, env=env)
    assert not w.is_error, w.text
    target = tmp_path / "notes" / "hello.txt"
    assert target.read_text(encoding="utf-8") == "alpha\nbeta\n"

    r = read.run({"path": str(target)}, env=env)
    assert not r.is_error, r.text
    assert "alpha" in r.text and "beta" in r.text

    e = edit.run(
        {"path": str(target), "edits": [{"oldText": "beta", "newText": "gamma"}]},
        env=env,
    )
    assert not e.is_error, e.text

    # Relative path resolves against HOME, same file.
    r2 = read.run({"path": "notes/hello.txt"}, env=env)
    assert not r2.is_error, r2.text
    assert "gamma" in r2.text and "beta" not in r2.text


def test_edit_requires_unique_match(tmp_path: Path):
    target = tmp_path / "dup.txt"
    target.write_text("same\nsame\n", encoding="utf-8")
    e = edit.run(
        {"path": str(target), "edits": [{"oldText": "same", "newText": "other"}]},
        env={"HOME": str(tmp_path)},
    )
    assert e.is_error
    assert "2 occurrences" in e.text


def test_read_missing_file_is_error(tmp_path: Path):
    r = read.run({"path": str(tmp_path / "nope.txt")}, env={"HOME": str(tmp_path)})
    assert r.is_error
    assert "ENOENT" in r.text


# ---------------------------------------------------------------------------
# bash tool


def test_bash_runs_command_and_returns_output(tmp_path: Path):
    res = _run(bash.run({"command": "echo hello; echo oops 1>&2; exit 3"}))
    assert res.exit_code == 3
    assert res.stdout.strip() == "hello"
    assert "oops" in res.stderr
    assert "[exit 3]" in res.render()


def test_bash_cwd_and_env_override(tmp_path: Path):
    res = _run(
        bash.run({"command": "pwd; echo $PAI_TEST_MARK", "cwd": str(tmp_path)},
                 env={"PAI_TEST_MARK": "mark42"})
    )
    assert res.exit_code == 0
    lines = res.stdout.strip().splitlines()
    assert Path(lines[0]).resolve() == tmp_path.resolve()
    assert lines[1] == "mark42"


def test_bash_default_cwd_is_home(tmp_path: Path):
    res = _run(bash.run({"command": "pwd"}, env={"HOME": str(tmp_path)}))
    assert res.exit_code == 0
    assert Path(res.stdout.strip()).resolve() == tmp_path.resolve()


def _wait_for_death(pid: int) -> bool:
    """Poll `pid` for up to 3s. Returns True once it is gone; on survival,
    SIGKILLs it so a failing test doesn't leak a 30s sleeper."""
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    return False


def test_bash_timeout_kills_whole_process_group(tmp_path: Path):
    """Timeout must signal the process *group*, not just the bash child —
    a backgrounded grandchild used to survive the kill orphaned to PPID 1,
    holding the output pipes open and blocking the timeout path itself."""
    pidfile = tmp_path / "pid"
    started = time.monotonic()
    res = _run(
        bash.run(
            {
                "command": f"sh -c 'sleep 30 & echo $! > {pidfile}; sleep 30'",
                "timeout_ms": 1000,
                "cwd": str(tmp_path),
            }
        )
    )
    elapsed = time.monotonic() - started
    assert res.exit_code == -1
    assert "timed out" in res.stderr
    # A surviving grandchild holds stdout/stderr open, so the pre-killpg
    # code also *blocked* here until the orphan exited (~30s).
    assert elapsed < 5, f"timeout path blocked {elapsed:.1f}s on the orphan's pipes"
    grandchild = int(pidfile.read_text().strip())
    assert _wait_for_death(grandchild), "grandchild survived the timeout kill"


def test_bash_cancel_kills_whole_process_group(tmp_path: Path):
    """Interrupt cancels the turn task mid-tool-call; the CancelledError
    path must reap the spawned tree instead of leaking it."""
    pidfile = tmp_path / "pid"

    async def scenario() -> int:
        task = asyncio.create_task(
            bash.run(
                {
                    "command": f"sleep 30 & echo $! > {pidfile}; wait",
                    "cwd": str(tmp_path),
                }
            )
        )
        while not (pidfile.exists() and pidfile.read_text().strip()):
            await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return int(pidfile.read_text().strip())

    grandchild = _run(scenario())
    assert _wait_for_death(grandchild), "grandchild survived cancellation"


# ---------------------------------------------------------------------------
# truncate


def test_truncate_tail_keeps_the_end():
    content = "\n".join(f"line{i}" for i in range(3000))
    t = truncate.truncate_tail(content)
    assert t.truncated and t.truncated_by == "lines"
    assert t.output_lines == truncate.DEFAULT_MAX_LINES
    assert t.content.splitlines()[0] == "line1000"
    assert t.content.splitlines()[-1] == "line2999"


def test_truncate_head_keeps_the_start():
    content = "\n".join(f"line{i}" for i in range(3000))
    t = truncate.truncate_head(content)
    assert t.truncated and t.truncated_by == "lines"
    assert t.content.splitlines()[0] == "line0"
    assert t.content.splitlines()[-1] == "line1999"


def test_truncate_passthrough_untouched():
    t = truncate.truncate_tail("short\noutput\n")
    assert not t.truncated
    assert t.content == "short\noutput\n"


def test_cap_tail_for_model_spills_full_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    rendered = "\n".join(f"line{i}" for i in range(3000))
    out = truncate.cap_tail_for_model(rendered, tool="bash")
    assert "Full output: " in out
    assert out.startswith("line1000")
    spill_path = Path(out.rsplit("Full output: ", 1)[1].rstrip("]"))
    assert spill_path.parent == tmp_path
    assert spill_path.read_text(encoding="utf-8") == rendered
    assert f"lines 1001-3000 of 3000" in out


def test_cap_tail_for_model_passthrough_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    out = truncate.cap_tail_for_model("just fine", tool="bash")
    assert out == "just fine"
    assert list(tmp_path.iterdir()) == []
