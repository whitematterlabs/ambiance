from __future__ import annotations

from boot import main, paths


def test_reap_pgrp_uses_host_ps(monkeypatch) -> None:
    calls: list[list[str]] = []

    monkeypatch.setattr(paths, "host_executable", lambda name: "/host/bin/ps")
    monkeypatch.setattr(main.os, "getpid", lambda: 123)
    monkeypatch.setattr(main.os, "getpgrp", lambda: 123)
    monkeypatch.setattr(
        main.subprocess,
        "check_output",
        lambda cmd, **_kwargs: calls.append(cmd) or "123 123\n",
    )

    main._reap_pgrp()

    assert calls == [["/host/bin/ps", "-eo", "pid=,pgid="]]


def test_reap_descendants_uses_host_ps(monkeypatch) -> None:
    calls: list[list[str]] = []

    monkeypatch.setattr(paths, "host_executable", lambda name: "/host/bin/ps")
    monkeypatch.setattr(main.os, "getpid", lambda: 123)
    monkeypatch.setattr(
        main.subprocess,
        "check_output",
        lambda cmd, **_kwargs: calls.append(cmd) or "123 1\n",
    )

    main._reap_descendants()

    assert calls == [["/host/bin/ps", "-eo", "pid=,ppid="]]
