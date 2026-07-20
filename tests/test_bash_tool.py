"""Tests for the one-shot subprocess `bash` tool."""

from __future__ import annotations

import asyncio
import os
import signal
import time
from pathlib import Path

import pytest

from boot import bash_tool
from boot import paths


def _run(coro):
    return asyncio.run(coro)


def test_happy_path_stdout(tmp_path: Path):
    res = _run(bash_tool.run({"command": "echo hello"}, env={"PAI_HOME_OVERRIDE": "x"}))
    assert res.exit_code == 0
    assert res.stdout.strip() == "hello"
    assert res.stderr == ""


def test_exit_code_propagation():
    res = _run(bash_tool.run({"command": "exit 7"}))
    assert res.exit_code == 7


def test_stderr_captured():
    res = _run(bash_tool.run({"command": "echo oops 1>&2"}))
    assert res.exit_code == 0
    assert "oops" in res.stderr


def test_cwd_honored(tmp_path: Path):
    res = _run(bash_tool.run({"command": "pwd", "cwd": str(tmp_path)}))
    assert res.exit_code == 0
    assert Path(res.stdout.strip()).resolve() == tmp_path.resolve()


def test_home_env_drives_tilde_expansion(tmp_path: Path):
    """`~`/`$HOME` inside a PAI command must resolve to the HOME the kernel
    passes in env (the PAI's own home), not the host user's home. Regression:
    nudge used to omit HOME, so `save to ~/workspace` landed in the human's
    /Users/<me>/workspace instead of the PAI's workspace."""
    pai_home = tmp_path / "pai_home"
    pai_home.mkdir()
    res = _run(
        bash_tool.run(
            {"command": "echo $HOME; echo ~"},
            env={"PAI_SLUG": "pai", "HOME": str(pai_home)},
        )
    )
    assert res.exit_code == 0
    lines = res.stdout.strip().splitlines()
    assert Path(lines[0]).resolve() == pai_home.resolve()
    assert Path(lines[1]).resolve() == pai_home.resolve()


def test_cwd_must_exist(tmp_path: Path):
    bogus = tmp_path / "does_not_exist"
    res = _run(bash_tool.run({"command": "pwd", "cwd": str(bogus)}))
    assert res.exit_code == -1
    assert "does not exist" in res.stderr


def test_cwd_does_not_persist_across_calls(tmp_path: Path):
    """Each invocation is a fresh subprocess — `cd` in one call has no
    effect on the next."""
    sub = tmp_path / "sub"
    sub.mkdir()
    r1 = _run(bash_tool.run({"command": f"cd {sub} && pwd", "cwd": str(tmp_path)}))
    assert r1.exit_code == 0
    assert Path(r1.stdout.strip()).resolve() == sub.resolve()
    r2 = _run(bash_tool.run({"command": "pwd", "cwd": str(tmp_path)}))
    assert r2.exit_code == 0
    assert Path(r2.stdout.strip()).resolve() == tmp_path.resolve()


def test_timeout_kills_cleanly(tmp_path: Path):
    res = _run(bash_tool.run({"command": "sleep 5", "timeout_ms": 200, "cwd": str(tmp_path)}))
    assert res.exit_code == -1
    assert "timed out" in res.stderr


def _wait_for_death(probe, cleanup_sig_target) -> bool:
    """Poll `probe` (raises ProcessLookupError once dead) for up to 3s.
    Returns True if the target died; on survival, SIGKILLs it via
    `cleanup_sig_target` so a failing test doesn't leak a 30s sleeper."""
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        try:
            probe()
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    try:
        cleanup_sig_target()
    except ProcessLookupError:
        pass
    return False


def test_timeout_kills_whole_process_tree(tmp_path: Path):
    """Timeout must signal the process *group*, not just the bash child —
    a `cmd & wait` grandchild used to survive the kill orphaned to PPID 1
    (2026-07-07: an interrupted Mail.app osascript ran 15 extra minutes)."""
    pidfile = tmp_path / "pid"
    started = time.monotonic()
    res = _run(
        bash_tool.run(
            {
                "command": f"sleep 30 & echo $! > {pidfile}; wait",
                "timeout_ms": 1000,
                "cwd": str(tmp_path),
            }
        )
    )
    elapsed = time.monotonic() - started
    assert res.exit_code == -1
    # A surviving grandchild holds the stdout/stderr pipes open, so the
    # pre-fix code also *blocked* here until the orphan exited (~30s).
    assert elapsed < 5, f"timeout path blocked {elapsed:.1f}s on the orphan's pipes"
    grandchild = int(pidfile.read_text().strip())
    assert _wait_for_death(
        lambda: os.kill(grandchild, 0),
        lambda: os.kill(grandchild, signal.SIGKILL),
    ), "grandchild survived the timeout kill"


def test_cancel_kills_whole_process_tree(tmp_path: Path):
    """Owner interrupt cancels the nudge task mid-tool-call; the
    CancelledError path must reap the spawned tree instead of leaking it."""
    pidfile = tmp_path / "pid"

    async def scenario() -> int:
        task = asyncio.create_task(
            bash_tool.run(
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
    assert _wait_for_death(
        lambda: os.kill(grandchild, 0),
        lambda: os.kill(grandchild, signal.SIGKILL),
    ), "grandchild survived cancellation"


def test_fhs_illusion_rejected_with_hint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """`/etc/foo` that only exists under PAI_ROOT is refused, never rewritten."""
    fake_root = tmp_path / "fake_pai_root"
    (fake_root / "etc").mkdir(parents=True)
    (fake_root / "etc" / "marker").write_text("present\n")
    monkeypatch.setattr(bash_tool, "PAI_ROOT", fake_root, raising=True)

    res = _run(bash_tool.run({"command": "cat /etc/marker", "cwd": str(tmp_path)}))
    assert res.exit_code == -1
    assert f"{fake_root}/etc/marker" in res.stderr


def test_host_ps_wins_in_user_shell_path(tmp_path: Path):
    res = _run(bash_tool.run({"command": "command -v ps", "cwd": str(tmp_path)}))

    assert res.exit_code == 0
    assert res.stdout.strip() == paths.host_executable("ps")


def test_missing_command_field():
    res = _run(bash_tool.run({}))
    assert res.exit_code == -1
    assert "required" in res.stderr


def test_string_input_treated_as_command(tmp_path: Path):
    res = _run(bash_tool.run("echo via-string"))
    assert res.exit_code == 0
    assert res.stdout.strip() == "via-string"
