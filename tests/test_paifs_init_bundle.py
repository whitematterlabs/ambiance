"""Bundle-mode provisioning for paifs_init (PAI.app first-run path).

These exercise `lay_out(..., bundle_mode=True)` against a temp root with the
seed dir faked and `paiman`/registry mocked out. The contract under test:
  - seed content is COPIED, not symlinked (no repo to point at);
  - tool shims shebang the passed interpreter (the embedded python);
  - no `uv` / venv / pth machinery runs;
  - the .provisioned marker is written with the schema version.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from bin import paifs_init


@pytest.fixture
def seed(tmp_path: Path) -> Path:
    """A faithful (tiny) stand-in for Resources/seed: etc/ + doc/."""
    s = tmp_path / "seed"
    (s / "etc" / "boilerplate").mkdir(parents=True)
    (s / "etc" / "owner.md").write_text("# owner\n")
    (s / "etc" / "boilerplate" / "memory-usage.md").write_text("# memory-usage\n")
    (s / "etc" / "boilerplate" / "capability-escalation.md").write_text("# cap-esc\n")
    (s / "doc").mkdir()
    (s / "doc" / "FILESYSTEM_v3.md").write_text("# fs v3\n")
    return s


@pytest.fixture
def no_seed_kernel(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Stub out the paiman/registry seed step — first-run network clone is
    out of scope here. Records the roots it was called with."""
    calls: list[Path] = []
    monkeypatch.setattr(
        paifs_init, "seed_kernel_essentials", lambda root: calls.append(root)
    )
    return calls


@pytest.fixture
def forbid_dev_machinery(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bundle mode must touch none of the dev-venv/uv path."""
    def boom(*_a, **_k):  # noqa: ANN001, ANN002
        raise AssertionError("dev-only provisioning step ran in bundle mode")

    for name in ("_ensure_uv", "ensure_venv", "install_pth", "ensure_system_deps"):
        monkeypatch.setattr(paifs_init, name, boom)


def test_bundle_copies_seed_content_not_symlinks(
    tmp_path: Path, seed: Path, no_seed_kernel, forbid_dev_machinery
) -> None:
    root = tmp_path / "pai"
    paifs_init.lay_out(root, bundle_mode=True, seed=seed)

    doc = root / "usr" / "share" / "doc"
    assert doc.is_dir() and not doc.is_symlink()
    assert (doc / "FILESYSTEM_v3.md").read_text() == "# fs v3\n"

    owner = root / "etc" / "owner.md"
    assert owner.is_file() and not owner.is_symlink()
    assert owner.read_text() == "# owner\n"

    for rel in (
        "etc/boilerplate/owner.md",
        "etc/boilerplate/memory-usage.md",
        "etc/boilerplate/capability-escalation.md",
    ):
        p = root / rel
        assert p.is_file() and not p.is_symlink(), rel


def test_bundle_skips_code_symlinks(
    tmp_path: Path, seed: Path, no_seed_kernel, forbid_dev_machinery
) -> None:
    root = tmp_path / "pai"
    paifs_init.lay_out(root, bundle_mode=True, seed=seed)
    # Code lives in the bundle; these repo-pointing slots must not appear.
    assert not (root / "boot").exists()
    assert not (root / "usr" / "src").exists()
    # And no dev venv was laid down.
    assert not (root / "usr" / "lib" / "venv").exists()


def test_bundle_shims_shebang_embedded_python(
    tmp_path: Path, seed: Path, no_seed_kernel, forbid_dev_machinery
) -> None:
    root = tmp_path / "pai"
    paifs_init.lay_out(root, bundle_mode=True, seed=seed)

    # paiman is privileged → sbin; paictl is PAI-callable → usr/bin.
    paiman = root / "sbin" / "paiman"
    paictl = root / "usr" / "bin" / "paictl"
    assert paiman.is_file() and paictl.is_file()
    assert paiman.read_text().splitlines()[0] == f"#!{sys.executable}"
    assert paictl.read_text().splitlines()[0] == f"#!{sys.executable}"
    assert paiman.stat().st_mode & 0o111

    # The usr/bin/python exec-shim targets the embedded interpreter.
    py = root / "usr" / "bin" / "python"
    assert py.read_text() == f'#!/bin/sh\nexec "{sys.executable}" "$@"\n'


def test_bundle_writes_provisioned_marker(
    tmp_path: Path, seed: Path, no_seed_kernel, forbid_dev_machinery
) -> None:
    root = tmp_path / "pai"
    paifs_init.lay_out(root, bundle_mode=True, seed=seed)
    marker = root / "var" / "lib" / ".provisioned"
    assert marker.is_file()
    assert marker.read_text().strip() == str(paifs_init.PROVISION_SCHEMA)


def test_bundle_seeds_kernel_essentials(
    tmp_path: Path, seed: Path, no_seed_kernel, forbid_dev_machinery
) -> None:
    root = tmp_path / "pai"
    paifs_init.lay_out(root, bundle_mode=True, seed=seed)
    # The (mocked) registry seed still runs in bundle mode — same as dev.
    assert no_seed_kernel == [root]


def test_bundle_requires_seed(tmp_path: Path, forbid_dev_machinery) -> None:
    with pytest.raises(SystemExit, match="requires --seed"):
        paifs_init.lay_out(tmp_path / "pai", bundle_mode=True, seed=None)


def test_bundle_missing_seed_content_errors(
    tmp_path: Path, no_seed_kernel, forbid_dev_machinery
) -> None:
    empty = tmp_path / "empty-seed"
    empty.mkdir()
    with pytest.raises(SystemExit, match="seed content missing"):
        paifs_init.lay_out(tmp_path / "pai", bundle_mode=True, seed=empty)
