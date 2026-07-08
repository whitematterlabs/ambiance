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


def _seed_pair(
    repo: Path,
    registry: Path,
    *,
    kind: str,
    src_name: str,
    pkg: str | None = None,
    fname: str | None = None,
    src_body: str = "print('x')\n",
    reg_body: str | None = None,
    reg_kind: str | None = None,
) -> tuple[Path, Path]:
    """Create a src/<kind>/<src_name>.py and its registry copy."""
    src = repo / "src" / kind / f"{src_name}.py"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text(src_body)
    reg = (
        registry
        / (reg_kind or kind)
        / (pkg or src_name)
        / f"{fname or src_name}.py"
    )
    reg.parent.mkdir(parents=True, exist_ok=True)
    reg.write_text(reg_body if reg_body is not None else src_body)
    return src, reg


def test_discover_dual_homed_maps_empirically(tmp_path: Path) -> None:
    repo, registry = tmp_path / "repo", tmp_path / "registry"
    # Plain name, bin kind.
    s1, r1 = _seed_pair(repo, registry, kind="bin", src_name="clear")
    # Underscore src ↔ hyphenated registry dir keeping the underscore file
    # (the send_message.py ↔ bin/send-message/send_message.py shape).
    s2, r2 = _seed_pair(
        repo, registry, kind="bin", src_name="send_message", pkg="send-message"
    )
    # Hyphenated dir *and* file (the emit_event.py ↔ sbin/emit-event/ shape).
    s3, r3 = _seed_pair(
        repo, registry, kind="sbin", src_name="emit_event", pkg="emit-event"
    )
    # Not dual-homed: no registry package → excluded from the mapping.
    only_src = repo / "src" / "sbin" / "pairelease.py"
    only_src.write_text("print('release')\n")
    # __init__.py is a package artifact, not a tool.
    (repo / "src" / "bin" / "__init__.py").write_text("")

    pairs = pairelease.discover_dual_homed(repo, registry)

    assert (s1, r1) in pairs
    assert (s2, r2) in pairs
    assert (s3, r3) in pairs
    assert not any(src == only_src for src, _ in pairs)
    assert not any(src.stem == "__init__" for src, _ in pairs)


def test_find_dual_homed_drift_flags_byte_differences(tmp_path: Path) -> None:
    repo, registry = tmp_path / "repo", tmp_path / "registry"
    _seed_pair(repo, registry, kind="bin", src_name="ps")  # identical
    s, r = _seed_pair(
        repo,
        registry,
        kind="bin",
        src_name="clear",
        src_body="import os\n",
        reg_body="# stale\n",
    )

    assert pairelease.find_dual_homed_drift(repo, registry) == [(s, r)]


def test_check_dual_homed_drift_hard_fails_with_both_paths(tmp_path: Path) -> None:
    repo, registry = tmp_path / "repo", tmp_path / "registry"
    s, r = _seed_pair(
        repo,
        registry,
        kind="bin",
        src_name="clear",
        src_body="new\n",
        reg_body="old\n",
    )

    with pytest.raises(SystemExit) as exc:
        pairelease.check_dual_homed_drift(repo, registry)

    msg = str(exc.value)
    # Actionable: names the tool, both absolute paths, and the escape hatch.
    assert str(s) in msg
    assert str(r) in msg
    assert "--skip-drift-check" in msg


def test_check_dual_homed_drift_passes_when_in_sync(tmp_path: Path) -> None:
    repo, registry = tmp_path / "repo", tmp_path / "registry"
    _seed_pair(repo, registry, kind="bin", src_name="clear")
    _seed_pair(repo, registry, kind="sbin", src_name="reboot")

    # No drift → returns normally (no SystemExit).
    pairelease.check_dual_homed_drift(repo, registry)


def test_check_dual_homed_drift_warns_when_registry_missing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    (repo / "src" / "bin").mkdir(parents=True)

    # A dev box without the pairegistry checkout must not be blocked — the
    # check degrades to a loud warning rather than a hard failure.
    pairelease.check_dual_homed_drift(repo, tmp_path / "nowhere")

    assert "skipping dual-homed drift check" in capsys.readouterr().err


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
