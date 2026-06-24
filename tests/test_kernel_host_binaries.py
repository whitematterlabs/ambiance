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


def test_ad_hoc_subagent_classifier() -> None:
    assert main._is_ad_hoc_subagent_spec({"kind": "pai", "parent": 2})
    assert not main._is_ad_hoc_subagent_spec({"kind": "pai"})
    assert not main._is_ad_hoc_subagent_spec({"kind": "pai", "parent": 2, "persub": True})
    assert not main._is_ad_hoc_subagent_spec({"kind": "pai", "parent": 2, "run": "worker"})
    assert not main._is_ad_hoc_subagent_spec({"kind": "pai", "parent": 2, "schedule": "0 9 * * *"})
    assert not main._is_ad_hoc_subagent_spec({"kind": "driver", "parent": 2})
