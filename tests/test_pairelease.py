from __future__ import annotations

import io
import tarfile
import urllib.error
from pathlib import Path

import pytest

from sbin import pairelease


class _FakeResp(io.BytesIO):
    def __enter__(self):  # urlopen is used as a context manager
        return self

    def __exit__(self, *exc) -> bool:
        self.close()
        return False


def test_parse_build_number() -> None:
    assert pairelease.parse_build_number("0.1.0+build.42") == 42
    assert pairelease.parse_build_number("0.1.0+build.9\n") == 9
    # No counter suffix → 0 (base semver or hand-cut release).
    assert pairelease.parse_build_number("0.1.0") == 0
    assert pairelease.parse_build_number("") == 0


def test_next_build_number_increments_published(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        pairelease.urllib.request,
        "urlopen",
        lambda *a, **k: _FakeResp(b"0.1.0+build.7\n"),
    )
    assert pairelease.next_build_number("https://example/latest") == 8


def test_next_build_number_first_publish_on_404(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(*a, **k):
        raise urllib.error.HTTPError("u", 404, "Not Found", {}, None)

    monkeypatch.setattr(pairelease.urllib.request, "urlopen", _raise)
    # No release yet → first build is 1, not an error.
    assert pairelease.next_build_number("https://example/latest") == 1


def test_next_build_number_aborts_on_transient_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise(*a, **k):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(pairelease.urllib.request, "urlopen", _raise)
    # A non-404 failure must abort rather than reset the counter to 1.
    with pytest.raises(SystemExit):
        pairelease.next_build_number("https://example/latest")


def test_read_version_from_pyproject(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "pai"\nversion = "1.2.3"\n'
    )
    assert pairelease.read_version(tmp_path) == "1.2.3"


def test_read_version_missing_is_fatal(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "pai"\n')
    with pytest.raises(SystemExit):
        pairelease.read_version(tmp_path)


def test_prune_staging_drops_dev_trees_only(tmp_path: Path) -> None:
    staging = tmp_path / "stage"
    # Dev-only trees that must be pruned.
    for name in pairelease.PRUNE_DIRS:
        (staging / name).mkdir(parents=True)
        (staging / name / "x").write_text("dev")
    # Runtime trees that must survive.
    (staging / "src" / "usr" / "share" / "doc").mkdir(parents=True)
    (staging / "src" / "usr" / "share" / "doc" / "FILESYSTEM.md").write_text("keep")
    (staging / "pyproject.toml").write_text("keep")
    (staging / "uv.lock").write_text("keep")
    (staging / "README.md").write_text("keep")

    removed = pairelease.prune_staging(staging)

    assert set(removed) == set(pairelease.PRUNE_DIRS)
    for name in pairelease.PRUNE_DIRS:
        assert not (staging / name).exists()
    # Runtime PAI docs under src/ are kept even though a top-level docs/ is pruned.
    assert (staging / "src" / "usr" / "share" / "doc" / "FILESYSTEM.md").exists()
    assert (staging / "pyproject.toml").exists()
    assert (staging / "uv.lock").exists()


def test_make_tarball_has_no_wrapping_dir(tmp_path: Path) -> None:
    staging = tmp_path / "stage"
    (staging / "src" / "boot").mkdir(parents=True)
    (staging / "src" / "boot" / "main.py").write_text("x")
    (staging / "pyproject.toml").write_text("y")

    out = tmp_path / "pai.tar.gz"
    pairelease.make_tarball(staging, out)

    with tarfile.open(out) as tf:
        names = tf.getnames()
    # Top-level entries land at the archive root (extract drops them straight
    # into the destination version dir), not under a pai-<ver>/ prefix.
    assert "pyproject.toml" in names
    assert "src/boot/main.py" in names
    assert not any(n.startswith("pai-") for n in names)
