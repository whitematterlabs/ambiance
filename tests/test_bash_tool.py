"""Tests for the one-shot subprocess `bash` tool."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from boot import bash_tool


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


def test_fhs_rewrite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """`/etc/foo` should rewrite to `$PAI_ROOT/etc/foo`."""
    fake_root = tmp_path / "fake_pai_root"
    (fake_root / "etc").mkdir(parents=True)
    (fake_root / "etc" / "marker").write_text("present\n")
    monkeypatch.setattr(bash_tool, "PAI_ROOT", fake_root, raising=True)

    res = _run(bash_tool.run({"command": "cat /etc/marker", "cwd": str(tmp_path)}))
    assert res.exit_code == 0
    assert res.stdout.strip() == "present"


def test_missing_command_field():
    res = _run(bash_tool.run({}))
    assert res.exit_code == -1
    assert "required" in res.stderr


def test_string_input_treated_as_command(tmp_path: Path):
    res = _run(bash_tool.run("echo via-string"))
    assert res.exit_code == 0
    assert res.stdout.strip() == "via-string"
