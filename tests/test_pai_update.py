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


def test_start_update_check_shows_ready_notice_when_behind(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        pai,
        "_read_update_status",
        lambda repo, *, fetch: _status(repo, behind=2),
    )

    pai._check_for_update_on_start()

    out = capsys.readouterr().out
    assert pai.UPDATE_READY_NOTICE in out
    assert "status: update available (2 commit(s) behind)" in out


# ---------- tarball (end-user) update path ----------

def _seed_marker(root: Path, ver: str) -> None:
    (root / "var" / "lib").mkdir(parents=True, exist_ok=True)
    (root / "var" / "lib" / ".release").write_text(f"{ver}\n")


def test_release_marker_absent_is_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pai, "PAI_ROOT", tmp_path / "pai")
    assert pai._release_marker() is None


def test_update_tarball_check_reports_available(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "pai"
    _seed_marker(root, "0.1.0")
    monkeypatch.setattr(pai, "PAI_ROOT", root)
    monkeypatch.setattr(pai, "_latest_release_version", lambda base: "0.2.0")

    assert pai.main(["update", "--check"]) == 0

    out = capsys.readouterr().out
    assert "installed: 0.1.0" in out
    assert "status: update available (0.2.0)" in out
    assert "next: pai update" in out


def test_update_tarball_check_up_to_date(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "pai"
    _seed_marker(root, "0.2.0")
    monkeypatch.setattr(pai, "PAI_ROOT", root)
    monkeypatch.setattr(pai, "_latest_release_version", lambda base: "0.2.0")

    assert pai.main(["update", "--check"]) == 0
    assert "status: up to date" in capsys.readouterr().out


def test_update_tarball_downloads_and_repoints(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "pai"
    _seed_marker(root, "0.1.0")
    (root / "opt" / "pai" / "0.1.0").mkdir(parents=True)
    monkeypatch.setattr(pai, "PAI_ROOT", root)
    monkeypatch.setattr(pai, "_latest_release_version", lambda base: "0.2.0")

    extracted: list[str] = []

    def fake_extract(base: str, ver: str) -> Path:
        d = root / "opt" / "pai" / ver
        d.mkdir(parents=True, exist_ok=True)
        extracted.append(ver)
        return d

    reprov: list[Path] = []
    monkeypatch.setattr(pai, "_download_and_extract", fake_extract)
    monkeypatch.setattr(
        pai, "_reprovision_tarball", lambda d: reprov.append(d) or 0
    )

    assert pai.main(["update"]) == 0

    assert extracted == ["0.2.0"]
    assert reprov == [root / "opt" / "pai" / "0.2.0"]
    assert (root / "opt" / "pai" / "current").resolve() == root / "opt" / "pai" / "0.2.0"
    assert (root / "var" / "lib" / ".release").read_text().strip() == "0.2.0"


def test_update_tarball_rollback_picks_prior(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import os

    root = tmp_path / "pai"
    opt = root / "opt" / "pai"
    (opt / "0.1.0").mkdir(parents=True)
    (opt / "0.2.0").mkdir(parents=True)
    # Make 0.2.0 the newer dir so rollback targets 0.1.0.
    os.utime(opt / "0.1.0", (1000, 1000))
    os.utime(opt / "0.2.0", (2000, 2000))
    _seed_marker(root, "0.2.0")
    monkeypatch.setattr(pai, "PAI_ROOT", root)

    reprov: list[Path] = []
    monkeypatch.setattr(
        pai, "_reprovision_tarball", lambda d: reprov.append(d) or 0
    )

    assert pai.main(["update", "--rollback"]) == 0

    assert reprov == [opt / "0.1.0"]
    assert (opt / "current").resolve() == opt / "0.1.0"
    assert (root / "var" / "lib" / ".release").read_text().strip() == "0.1.0"


def test_rollback_rejected_without_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(pai, "PAI_ROOT", tmp_path / "pai")
    assert pai.main(["update", "--rollback"]) == 1
    assert "tarball installs only" in capsys.readouterr().err
