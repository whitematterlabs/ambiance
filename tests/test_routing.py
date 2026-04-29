"""wake_on routing + per-PAI prompt wiring."""

from __future__ import annotations

from pathlib import Path

import pytest

from boot import bootstrap, main as M
from boot import processes as P


def _spawn(
    slug: str,
    *,
    pid: int,
    wake_on: list[str] | None = None,
    fallback: bool | None = None,
) -> None:
    P.spawn_pai(
        pid=pid,
        slug=slug,
        description=f"{slug} test",
        wake_on=wake_on,
        fallback=fallback,
    )


def test_route_explicit_match_excludes_fallback(live_dir: Path) -> None:
    # kernel_manager claims kernel:*, pai is fallback → only kernel_manager fires.
    _spawn("kernel_manager", pid=1, wake_on=["kernel:*"])
    _spawn("pai", pid=2, fallback=True)
    assert M._route_to_pids("kernel:reload_failed") == [1]


def test_route_falls_through_to_fallback(live_dir: Path) -> None:
    _spawn("kernel_manager", pid=1, wake_on=["kernel:*"])
    _spawn("pai", pid=2, fallback=True)
    # Nothing matches imessage:new → fallback PAI fires.
    assert M._route_to_pids("imessage:new") == [2]


def test_route_multiple_explicit_fanout(live_dir: Path) -> None:
    _spawn("a", pid=3, wake_on=["imessage:*"])
    _spawn("b", pid=4, wake_on=["imessage:new"])
    _spawn("pai", pid=2, fallback=True)
    # Both a and b match; fallback is suppressed.
    assert M._route_to_pids("imessage:new") == [3, 4]


def test_route_no_fallback_uses_default_pid(live_dir: Path) -> None:
    _spawn("kernel_manager", pid=1, wake_on=["kernel:*"])
    # No fallback PAI, no match → default fallback_pid.
    assert M._route_to_pids("imessage:new", fallback_pid=7) == [7]


def test_route_skips_non_running_fallback(live_dir: Path) -> None:
    _spawn("kernel_manager", pid=1, wake_on=["kernel:*"])
    _spawn("pai", pid=2, fallback=True)
    P.resolve("pai", "cancelled")
    assert M._route_to_pids("imessage:new", fallback_pid=99) == [99]


def test_route_multiple_fallbacks_all_fire(live_dir: Path) -> None:
    _spawn("a", pid=2, fallback=True)
    _spawn("b", pid=3, fallback=True)
    assert M._route_to_pids("imessage:new") == [2, 3]


def test_route_fallback_with_wake_on_match(live_dir: Path) -> None:
    # A PAI can have both wake_on and fallback. If wake_on matches it
    # fires via wake_on — fallback is only used when *no one else* matched.
    _spawn("a", pid=2, wake_on=["imessage:*"], fallback=True)
    assert M._route_to_pids("imessage:new") == [2]
    assert M._route_to_pids("kernel:foo") == [2]  # via fallback path


def test_build_system_prompt_includes_role(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    role = tmp_path / "role.md"
    role.write_text("you are the test role\n")
    monkeypatch.setattr(bootstrap, "REPO_ROOT", tmp_path, raising=True)
    bootstrap.build_system_prompt.cache_clear()
    out = bootstrap.build_system_prompt(pai=2, prompt_path="role.md")
    assert "<role>\nyou are the test role\n</role>" in out


def test_build_system_prompt_no_role_when_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bootstrap, "REPO_ROOT", tmp_path, raising=True)
    bootstrap.build_system_prompt.cache_clear()
    out_none = bootstrap.build_system_prompt(pai=1, prompt_path=None)
    assert "<role>" not in out_none
    bootstrap.build_system_prompt.cache_clear()
    out_missing = bootstrap.build_system_prompt(pai=1, prompt_path="does/not/exist.md")
    assert "<role>" not in out_missing
