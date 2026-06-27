from __future__ import annotations

import tarfile
from pathlib import Path

import pytest

from sbin import pairelease


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
