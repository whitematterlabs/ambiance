from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from bin import paiman
from boot import config as C
from boot import paths


FIXTURES = Path(__file__).parent / "fixtures" / "paiman"


@pytest.fixture
def fhs_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "pai"
    (root / "usr" / "lib" / "pais").mkdir(parents=True)
    monkeypatch.setattr(paths, "PAI_ROOT", root, raising=True)
    monkeypatch.setattr(C, "PACKAGES_DIR", root / "usr" / "lib" / "pais", raising=True)
    monkeypatch.setenv("PAIMAN_REGISTRY", str(FIXTURES / "registry"))
    return root


# ---------- legacy scaffold (init) ----------

def test_init_creates_loadable_bundle(fhs_root: Path) -> None:
    assert paiman.main(["init", "email-pai"]) == 0
    bundle = fhs_root / "usr" / "lib" / "pais" / "email-pai"
    assert (bundle / "package.yaml").is_file()
    assert (bundle / "prompt.md").is_file()
    data = yaml.safe_load((bundle / "package.yaml").read_text())
    assert data["kind"] == "pai"
    assert C.resolve_package("email-pai")["kind"] == "pai"


def test_init_refuses_existing_bundle(fhs_root: Path) -> None:
    paiman.main(["init", "dup"])
    with pytest.raises(SystemExit, match="already exists"):
        paiman.main(["init", "dup"])


@pytest.mark.parametrize("bad", ["", ".hidden", "foo/bar"])
def test_init_rejects_invalid_names(fhs_root: Path, bad: str) -> None:
    with pytest.raises(SystemExit):
        paiman.main(["init", bad])


# ---------- install / remove for the 4 primitives ----------

def test_install_skill(fhs_root: Path) -> None:
    assert paiman.main(["install", str(FIXTURES / "testskill")]) == 0
    bundle = fhs_root / "opt" / "paiman" / "testskill"
    slot = fhs_root / "usr" / "lib" / "skills" / "testskill"
    assert bundle.is_dir()
    assert (bundle / "SKILL.md").is_file()
    assert slot.is_symlink()
    assert (slot / "SKILL.md").read_text().startswith("# testskill")


def test_install_prompt(fhs_root: Path) -> None:
    assert paiman.main(["install", str(FIXTURES / "testprompt")]) == 0
    slot = fhs_root / "usr" / "share" / "prompts" / "testprompt.md"
    assert slot.is_symlink()
    assert slot.read_text().startswith("# testprompt")


def test_install_bin(fhs_root: Path) -> None:
    assert paiman.main(["install", str(FIXTURES / "testbin")]) == 0
    slot = fhs_root / "usr" / "bin" / "testbin"
    assert slot.is_symlink()
    target = slot.resolve()
    assert target.is_file()
    # Executable bit set on entrypoint.
    assert target.stat().st_mode & 0o111


def test_install_driver(fhs_root: Path) -> None:
    assert paiman.main(["install", str(FIXTURES / "testdriver")]) == 0
    slot = fhs_root / "usr" / "lib" / "drivers" / "testdriver"
    assert slot.is_symlink()
    assert (slot / "events.yaml").is_file()


def test_reinstall_overwrites(fhs_root: Path, tmp_path: Path) -> None:
    paiman.main(["install", str(FIXTURES / "testskill")])
    bundle = fhs_root / "opt" / "paiman" / "testskill"
    # User-edits-in-place: drop a stray file, then reinstall.
    (bundle / "stray.txt").write_text("edit")
    paiman.main(["install", str(FIXTURES / "testskill")])
    assert not (bundle / "stray.txt").exists()
    assert (bundle / "SKILL.md").is_file()


def test_remove(fhs_root: Path) -> None:
    paiman.main(["install", str(FIXTURES / "testskill")])
    assert paiman.main(["remove", "testskill"]) == 0
    assert not (fhs_root / "opt" / "paiman" / "testskill").exists()
    assert not (fhs_root / "usr" / "lib" / "skills" / "testskill").exists()


def test_remove_unknown_fails(fhs_root: Path) -> None:
    with pytest.raises(SystemExit, match="not installed"):
        paiman.main(["remove", "ghost"])


def test_install_rejects_missing_manifest(fhs_root: Path, tmp_path: Path) -> None:
    bad = tmp_path / "bad-bundle"
    bad.mkdir()
    with pytest.raises(SystemExit, match="package.yaml"):
        paiman.main(["install", str(bad)])


def test_install_rejects_unknown_kind(fhs_root: Path, tmp_path: Path) -> None:
    bad = tmp_path / "bad-bundle"
    bad.mkdir()
    (bad / "package.yaml").write_text("name: bad\nkind: nonsense\n")
    with pytest.raises(SystemExit, match="kind"):
        paiman.main(["install", str(bad)])


def test_install_bin_requires_entrypoint(fhs_root: Path, tmp_path: Path) -> None:
    bad = tmp_path / "bad-bin"
    bad.mkdir()
    (bad / "package.yaml").write_text("name: badbin\nkind: bin\n")
    with pytest.raises(SystemExit, match="entrypoint"):
        paiman.main(["install", str(bad)])


def test_audit_log_appends(fhs_root: Path) -> None:
    paiman.main(["install", str(FIXTURES / "testskill")])
    paiman.main(["remove", "testskill"])
    log = fhs_root / "var" / "lib" / "paiman" / "log.md"
    assert log.is_file()
    content = log.read_text()
    assert "install skill testskill" in content
    assert "remove skill testskill" in content


def test_show_installed(fhs_root: Path, capsys: pytest.CaptureFixture) -> None:
    paiman.main(["install", str(FIXTURES / "testskill")])
    capsys.readouterr()
    assert paiman.main(["show", "testskill"]) == 0
    out = capsys.readouterr().out
    assert "kind: skill" in out


# ---------- registry resolution ----------

def test_install_bare_name_resolves_via_registry(fhs_root: Path) -> None:
    assert paiman.main(["install", "testskill1"]) == 0
    assert (fhs_root / "opt" / "paiman" / "testskill1").is_dir()
    assert (fhs_root / "usr" / "lib" / "skills" / "testskill1").is_symlink()


def test_install_bare_name_unknown_fails(fhs_root: Path) -> None:
    with pytest.raises(SystemExit, match="not found in registry"):
        paiman.main(["install", "no-such-package"])


# ---------- pai install with deps ----------

def test_install_pai_pulls_deps_from_registry(fhs_root: Path) -> None:
    assert paiman.main(["install", str(FIXTURES / "testpai")]) == 0
    # pai bundle activated.
    assert (fhs_root / "usr" / "lib" / "pais" / "testpai").is_symlink()
    # All three deps installed and activated.
    assert (fhs_root / "usr" / "lib" / "skills" / "testskill1").is_symlink()
    assert (fhs_root / "usr" / "bin" / "testbin1").is_symlink()
    assert (fhs_root / "usr" / "share" / "prompts" / "testprompt1.md").is_symlink()


def test_install_pai_skips_existing_deps(fhs_root: Path) -> None:
    paiman.main(["install", "testskill1"])
    # User edits the installed skill.
    skill_dir = fhs_root / "opt" / "paiman" / "testskill1"
    (skill_dir / "user-edit.md").write_text("hand-tweaked")
    paiman.main(["install", str(FIXTURES / "testpai")])
    # Edit must survive — paiman skipped reinstalling the existing dep.
    assert (skill_dir / "user-edit.md").is_file()


def test_install_pai_dep_missing_from_registry_falls_through_to_pip(
    fhs_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Names not found in the registry are treated as PyPI packages and
    handed to the kernel venv's pip. The bundle install itself still
    completes; pip is invoked once at the end with the accumulated set."""
    calls: list[list[str]] = []

    def fake_run(cmd, check):  # type: ignore[no-untyped-def]
        calls.append(cmd)
        class _R:
            returncode = 0
        return _R()

    monkeypatch.setattr(paiman.subprocess, "run", fake_run)
    # Provision the kernel venv python so _pip_install doesn't bail.
    venv_bin = fhs_root / "usr" / "lib" / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "python").write_text("#!/bin/sh\n")

    pkg = tmp_path / "needs-pip"
    pkg.mkdir()
    (pkg / "package.yaml").write_text(
        "name: needspip\nkind: pai\ndeps: [some-pypi-pkg]\n"
    )
    assert paiman.main(["install", str(pkg)]) == 0
    assert (fhs_root / "opt" / "paiman" / "needspip").is_dir()
    # pip was invoked exactly once with the unresolved dep.
    assert len(calls) == 1
    assert "some-pypi-pkg" in calls[0]
    assert "install" in calls[0]


def test_install_pai_rejects_non_string_dep(
    fhs_root: Path, tmp_path: Path
) -> None:
    bad = tmp_path / "bad-pai"
    bad.mkdir()
    (bad / "package.yaml").write_text(
        "name: badpai\nkind: pai\ndeps:\n  - {name: foo}\n"
    )
    with pytest.raises(SystemExit, match="must be strings"):
        paiman.main(["install", str(bad)])


# ---------- remove dep-check ----------

def test_remove_refuses_when_pai_depends(fhs_root: Path) -> None:
    paiman.main(["install", str(FIXTURES / "testpai")])
    with pytest.raises(SystemExit, match="required by pai bundle"):
        paiman.main(["remove", "testskill1"])
    # Still installed.
    assert (fhs_root / "opt" / "paiman" / "testskill1").is_dir()


def test_remove_force_overrides_dep_check(fhs_root: Path) -> None:
    paiman.main(["install", str(FIXTURES / "testpai")])
    assert paiman.main(["remove", "--force", "testskill1"]) == 0
    assert not (fhs_root / "opt" / "paiman" / "testskill1").exists()


def test_remove_pai_does_not_remove_deps(fhs_root: Path) -> None:
    paiman.main(["install", str(FIXTURES / "testpai")])
    paiman.main(["remove", "testpai"])
    # Pai gone; primitives stay.
    assert not (fhs_root / "opt" / "paiman" / "testpai").exists()
    assert (fhs_root / "opt" / "paiman" / "testskill1").is_dir()


def test_list_installed(fhs_root: Path, capsys: pytest.CaptureFixture) -> None:
    paiman.main(["install", str(FIXTURES / "testskill")])
    paiman.main(["install", str(FIXTURES / "testbin")])
    capsys.readouterr()
    paiman.main(["list"])
    out = capsys.readouterr().out
    assert "testskill" in out
    assert "testbin" in out
