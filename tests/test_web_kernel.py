from __future__ import annotations

import fcntl
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from usr.libexec.web.pai_web import actions, server


def test_kernel_status_reports_held_pid_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lock_path = tmp_path / "run" / "kernel.pid"
    lock_path.parent.mkdir(parents=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        os.write(fd, b"4242\n")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        monkeypatch.setattr(actions, "_KERNEL_LOCK_FILE", lock_path, raising=True)

        assert actions.kernel_status() == {"running": True, "pid": "4242"}

        fcntl.flock(fd, fcntl.LOCK_UN)
        assert actions.kernel_status() == {"running": False, "pid": None}
    finally:
        os.close(fd)


def test_start_kernel_spawns_boot_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    popen_calls: list[tuple[tuple, dict]] = []

    class FakePopen:
        def __init__(self, *args, **kwargs) -> None:
            popen_calls.append((args, kwargs))

    monkeypatch.setattr(actions, "PAI_ROOT", tmp_path, raising=True)
    monkeypatch.setattr(actions, "check_layout", lambda root: [])
    monkeypatch.setattr(actions, "kernel_status", lambda: {"running": False, "pid": None})
    monkeypatch.setattr(
        actions,
        "_wait_for_kernel",
        lambda running, timeout=4.0: {"running": True, "pid": "99"},
    )
    monkeypatch.setattr(actions.subprocess, "Popen", FakePopen)

    assert actions.start_kernel() == {"running": True, "pid": "99"}

    assert len(popen_calls) == 1
    args, kwargs = popen_calls[0]
    assert args[0] == [sys.executable, "-u", "-m", "boot.entry"]
    assert kwargs["env"]["PAI_ROOT"] == str(tmp_path)
    assert str(tmp_path / "usr" / "lib") in kwargs["env"]["PYTHONPATH"].split(os.pathsep)
    assert str(tmp_path / "usr" / "src") in kwargs["env"]["PYTHONPATH"].split(os.pathsep)
    assert kwargs["env"]["PATH"].startswith(str(tmp_path / "usr" / "lib" / "venv" / "bin"))
    assert kwargs["start_new_session"] is True
    assert kwargs["stderr"] == subprocess.STDOUT
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["stdout"].name == str(tmp_path / "var" / "log" / "kernel" / "kernel.log")
    kwargs["stdout"].close()


def test_start_kernel_prefers_fhs_python(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    popen_calls: list[tuple[tuple, dict]] = []
    fhs_python = tmp_path / "usr" / "lib" / "venv" / "bin" / "python"
    fhs_python.parent.mkdir(parents=True)
    fhs_python.write_text("# fake python\n", encoding="utf-8")

    class FakePopen:
        def __init__(self, *args, **kwargs) -> None:
            popen_calls.append((args, kwargs))

    monkeypatch.setattr(actions, "PAI_ROOT", tmp_path, raising=True)
    monkeypatch.setattr(actions, "check_layout", lambda root: [])
    monkeypatch.setattr(actions, "kernel_status", lambda: {"running": False, "pid": None})
    monkeypatch.setattr(
        actions,
        "_wait_for_kernel",
        lambda running, timeout=4.0: {"running": True, "pid": "99"},
    )
    monkeypatch.setattr(actions.subprocess, "Popen", FakePopen)

    actions.start_kernel()

    args, kwargs = popen_calls[0]
    assert args[0] == [str(fhs_python), "-u", "-m", "boot.entry"]
    assert kwargs["env"]["PAI_ROOT"] == str(tmp_path)
    kwargs["stdout"].close()


def test_start_kernel_reports_boot_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakePopen:
        def __init__(self, *args, **kwargs) -> None:
            kwargs["stdout"].close()

    monkeypatch.setattr(actions, "PAI_ROOT", tmp_path, raising=True)
    monkeypatch.setattr(actions, "check_layout", lambda root: [])
    monkeypatch.setattr(actions, "kernel_status", lambda: {"running": False, "pid": None})
    monkeypatch.setattr(
        actions,
        "_wait_for_kernel",
        lambda running, timeout=4.0: {"running": False, "pid": None},
    )
    monkeypatch.setattr(actions.subprocess, "Popen", FakePopen)

    with pytest.raises(RuntimeError, match="kernel did not start"):
        actions.start_kernel()


def test_stop_kernel_sends_sigterm_to_kernel_process_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signals: list[tuple[int, signal.Signals]] = []

    monkeypatch.setattr(actions, "kernel_status", lambda: {"running": True, "pid": "123"})
    monkeypatch.setattr(actions.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(actions.os, "killpg", lambda pgid, sig: signals.append((pgid, sig)))
    monkeypatch.setattr(
        actions,
        "_wait_for_kernel",
        lambda running, timeout=4.0: {"running": False, "pid": None},
    )

    assert actions.stop_kernel() == {"running": False, "pid": None}
    assert signals == [(123, signal.SIGTERM)]


def test_stop_kernel_reports_failed_shutdown(monkeypatch: pytest.MonkeyPatch) -> None:
    signals: list[tuple[int, signal.Signals]] = []

    monkeypatch.setattr(actions, "kernel_status", lambda: {"running": True, "pid": "123"})
    monkeypatch.setattr(actions.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(actions.os, "killpg", lambda pgid, sig: signals.append((pgid, sig)))
    monkeypatch.setattr(
        actions,
        "_wait_for_kernel",
        lambda running, timeout=4.0: {"running": True, "pid": "123"},
    )

    with pytest.raises(RuntimeError, match="kernel did not stop"):
        actions.stop_kernel()
    assert signals == [(123, signal.SIGTERM), (123, signal.SIGKILL)]


def test_kernel_get_route_returns_json_status(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: dict = {}
    handler = server.Handler.__new__(server.Handler)
    handler.path = "/api/kernel"
    handler._json = lambda obj, status=200: sent.update({"obj": obj, "status": status})
    handler._static = lambda path: pytest.fail(f"unexpected static fallback for {path}")
    monkeypatch.setattr(
        server.actions,
        "kernel_status",
        lambda: {"running": True, "pid": "123"},
    )

    server.Handler.do_GET(handler)

    assert sent == {"obj": {"ok": True, "running": True, "pid": "123"}, "status": 200}


@pytest.mark.parametrize(
    ("action", "helper"),
    [("start", "start_kernel"), ("stop", "stop_kernel")],
)
def test_kernel_post_route_dispatches_action(
    action: str,
    helper: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: dict = {}
    handler = server.Handler.__new__(server.Handler)
    handler.path = "/api/kernel"
    handler._read_body = lambda: {"action": action}
    handler._json = lambda obj, status=200: sent.update({"obj": obj, "status": status})
    monkeypatch.setattr(
        server.actions,
        helper,
        lambda: {"running": action == "start", "pid": "456" if action == "start" else None},
    )

    server.Handler.do_POST(handler)

    assert sent == {
        "obj": {
            "ok": True,
            "running": action == "start",
            "pid": "456" if action == "start" else None,
        },
        "status": 200,
    }
