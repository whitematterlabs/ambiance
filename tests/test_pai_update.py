from __future__ import annotations

from pathlib import Path

import pytest

from bin import pai


def _status(
    repo: Path,
    *,
    upstream: str | None = "origin/main",
    ahead: int = 0,
    behind: int = 0,
    dirty: bool = False,
) -> pai.UpdateStatus:
    return pai.UpdateStatus(
        repo=repo,
        branch="main",
        upstream=upstream,
        ahead=ahead,
        behind=behind,
        dirty=dirty,
        remote_url="https://example.invalid/pai.git",
    )


def test_update_check_reports_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[bool] = []

    def fake_read(repo: Path, *, fetch: bool) -> pai.UpdateStatus:
        calls.append(fetch)
        return _status(repo)

    monkeypatch.setattr(pai, "_read_update_status", fake_read)

    assert pai.main(["update", "--check"]) == 0

    out = capsys.readouterr().out
    assert calls == [True]
    assert "branch: main -> origin/main" in out
    assert "status: up to date" in out


def test_update_check_can_skip_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[bool] = []

    def fake_read(repo: Path, *, fetch: bool) -> pai.UpdateStatus:
        calls.append(fetch)
        return _status(repo)

    monkeypatch.setattr(pai, "_read_update_status", fake_read)

    assert pai.main(["update", "--check", "--no-fetch"]) == 0

    assert calls == [False]


def test_update_refuses_dirty_checkout(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        pai,
        "_read_update_status",
        lambda repo, *, fetch: _status(repo, behind=1, dirty=True),
    )
    monkeypatch.setattr(
        pai,
        "_run_checked",
        lambda *args, **kwargs: pytest.fail("should not pull a dirty checkout"),
    )

    assert pai.main(["update"]) == 1

    err = capsys.readouterr().err
    assert "local changes" in err


def test_update_check_shows_next_step_when_behind(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        pai,
        "_read_update_status",
        lambda repo, *, fetch: _status(repo, behind=2),
    )

    assert pai.main(["update", "--check"]) == 0

    out = capsys.readouterr().out
    assert "status: update available (2 commit(s) behind)" in out
    assert "next: pai update" in out


def test_update_pulls_and_reprovisions_when_behind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pulled: list[list[str]] = []
    reprovisioned: list[tuple[Path, bool]] = []

    monkeypatch.setattr(
        pai,
        "_read_update_status",
        lambda repo, *, fetch: _status(repo, behind=2),
    )
    monkeypatch.setattr(
        pai,
        "_run_checked",
        lambda cmd, *, cwd: pulled.append(cmd),
    )
    monkeypatch.setattr(
        pai,
        "_reprovision_after_update",
        lambda repo, *, no_web: reprovisioned.append((repo, no_web)) or 0,
    )

    assert pai.main(["update", "--no-web"]) == 0

    assert pulled == [["git", "pull", "--ff-only"]]
    assert reprovisioned == [(pai.REPO_ROOT, True)]


def test_start_runs_update_check_before_layout_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    monkeypatch.setattr(
        pai,
        "_check_for_update_on_start",
        lambda: calls.append("update"),
    )
    monkeypatch.setattr(
        pai,
        "check_layout",
        lambda root: calls.append("layout") or ["etc"],
    )

    assert pai.main(["start"]) == 1

    assert calls == ["update", "layout"]


def test_start_update_check_failure_is_nonfatal(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail_read(repo: Path, *, fetch: bool) -> pai.UpdateStatus:
        raise SystemExit("network unavailable")

    monkeypatch.setattr(pai, "_read_update_status", fail_read)

    pai._check_for_update_on_start()

    captured = capsys.readouterr()
    assert "==> update check" in captured.out
    assert "update check skipped" in captured.err
    assert "network unavailable" in captured.err
