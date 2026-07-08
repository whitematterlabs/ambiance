from __future__ import annotations

import sys
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
    venv_bin = root / "usr" / "lib" / "venv" / "bin"
    venv_bin.mkdir(parents=True, exist_ok=True)
    py = venv_bin / "python"
    py.write_text("#!/bin/sh\nexit 0\n")
    py.chmod(0o755)
    monkeypatch.setattr(paths, "PAI_ROOT", root, raising=True)
    monkeypatch.setattr(C, "PACKAGES_DIR", root / "usr" / "lib" / "pais", raising=True)
    monkeypatch.setenv("PAIMAN_REGISTRY", str(FIXTURES / "registry"))
    return root


def test_local_tilde_paths_use_owner_home_not_env_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner_home = tmp_path / "owner"
    sandbox_home = tmp_path / "sandbox"
    registry = owner_home / "Projects" / "pairegistry"
    pkg = owner_home / "pkg"
    registry.mkdir(parents=True)
    pkg.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(sandbox_home))
    monkeypatch.setenv("PAIMAN_REGISTRY", "~/Projects/pairegistry")
    monkeypatch.setattr(paiman, "_real_home", lambda: owner_home, raising=True)

    reg = paiman._Registry(tmp_path / "work")

    assert reg.root() == registry
    assert paiman._resolve_source("~/pkg", reg, tmp_path / "work") == pkg


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
    bundle = fhs_root / "opt" / "paiman" / "skill" / "testskill"
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
    # Prompts must stay flat at opt/paiman/<name> (NOT opt/paiman/prompt/<name>):
    # config.yaml `prompt_dir` points at the bundle dir to glob its *.md, so
    # kind-grouping here would silently empty a PAI's role prompt.
    assert (fhs_root / "opt" / "paiman" / "testprompt").is_dir()


def test_install_bin(fhs_root: Path) -> None:
    assert paiman.main(["install", str(FIXTURES / "testbin")]) == 0
    slot = fhs_root / "usr" / "bin" / "testbin"
    # bin/sbin install now writes a shell shim that execs the bundle
    # entrypoint via the kernel venv python — not a symlink.
    assert slot.is_file()
    assert slot.stat().st_mode & 0o111
    shim = slot.read_text()
    assert shim.startswith("#!/bin/sh")
    bundle_entry = fhs_root / "opt" / "paiman" / "bin" / "testbin" / "bin" / "testbin.py"
    assert str(bundle_entry) in shim
    assert bundle_entry.is_file()
    # The shim MUST exec via the FHS venv python — the one interpreter that
    # holds hook-installed deps — never sys.executable (which on a fresh
    # install is a throwaway clone venv lacking those deps). Regression guard
    # for the whatsapp_pair `ModuleNotFoundError: qrcode` bug.
    assert f'exec "{paths.venv_python()}"' in shim
    assert sys.executable not in shim or sys.executable == str(paths.venv_python())


def test_install_driver(fhs_root: Path) -> None:
    assert paiman.main(["install", str(FIXTURES / "testdriver")]) == 0
    slot = fhs_root / "usr" / "lib" / "drivers" / "testdriver"
    skill_slot = fhs_root / "usr" / "lib" / "skills" / "drivers" / "testdriver"
    assert slot.is_symlink()
    assert (slot / "events.yaml").is_file()
    assert skill_slot.is_symlink()
    assert (skill_slot / "SKILL.md").read_text().startswith("---")


def test_install_subagent(fhs_root: Path) -> None:
    # Persistent subagents resolve from /usr/lib/subagents/<name>/ (see
    # src/bin/subagent.py); paiman installs them with the same symlink model
    # as drivers/pais.
    assert paiman.main(["install", str(FIXTURES / "testsubagent")]) == 0
    slot = fhs_root / "usr" / "lib" / "subagents" / "testsubagent"
    assert slot.is_symlink()
    assert (slot / "prompt.md").is_file()
    # Subagent kind stages under the PLURAL subagents/ so it can never nest
    # inside the flat `subagent` prompt bundle's dir (opt/paiman/subagent/) —
    # nesting there let a prompt reinstall rmtree the packages and made the
    # scanners skip them. Regression guard for that collision.
    assert (fhs_root / "opt" / "paiman" / "subagents" / "testsubagent").is_dir()
    assert not (fhs_root / "opt" / "paiman" / "subagent").exists()


@pytest.mark.parametrize("reserved", ["bin", "driver", "skill", "pai", "lib", "subagents"])
def test_flat_prompt_rejects_reserved_names(reserved: str) -> None:
    # A PROMPT stages flat at opt/paiman/<name>, so naming it after a group dir
    # collides. Rejected.
    with pytest.raises(SystemExit, match="may not be named"):
        paiman._reject_reserved_flat_name("prompt", reserved, None)


@pytest.mark.parametrize(
    "kind,name",
    [
        ("bin", "pai"),          # bin/pai — the real launcher bin, groups safely
        ("pai", "pai"),          # pai/pai — leaf under the pai group dir
        ("subagent", "browse"),  # subagents/browse
        ("driver", "driver"),    # driver/driver
        ("prompt", "subagent"),  # singular: no longer a group dir → safe flat
        ("prompt", "pai_default"),
    ],
)
def test_reserved_guard_allows_grouped_and_safe_names(kind: str, name: str) -> None:
    # Kind-grouped bundles nest as <kind>/<name>, never colliding; and a flat
    # prompt whose name isn't a group dir is fine. None of these raise.
    paiman._reject_reserved_flat_name(kind, name, None)


def test_reserved_names_match_opt_rel() -> None:
    # Drift guard: the blacklist must equal exactly the set of first-segment
    # group-dir names _opt_rel emits for kind-scoped bundles. Add a kind or
    # change its staging without updating RESERVED_BUNDLE_NAMES and this fails
    # rather than silently reopening the collision hole. `subagent` (singular,
    # a real prompt bundle) must NOT be reserved; `subagents` (the plural group
    # dir) must be.
    emitted = {
        rel.split("/", 1)[0]
        for kind in paiman.INSTALLABLE_KINDS
        if "/" in (rel := paiman._opt_rel(kind, "x", None))
    }
    assert paiman.RESERVED_BUNDLE_NAMES == emitted
    assert "subagents" in paiman.RESERVED_BUNDLE_NAMES
    assert "subagent" not in paiman.RESERVED_BUNDLE_NAMES


def test_install_subagent_pulls_deps_from_registry(
    fhs_root: Path, tmp_path: Path
) -> None:
    pkg = tmp_path / "subagent-with-deps"
    pkg.mkdir()
    (pkg / "package.yaml").write_text(
        "name: subagent-with-deps\n"
        "kind: subagent\n"
        "version: 0.1.0\n"
        "prompt: prompt.md\n"
        "deps:\n"
        "  - bin/testbin1\n"
    )
    (pkg / "prompt.md").write_text("Role prompt.")

    assert paiman.main(["install", str(pkg)]) == 0

    assert (fhs_root / "usr" / "lib" / "subagents" / "subagent-with-deps").is_symlink()
    assert (fhs_root / "usr" / "bin" / "testbin1").is_file()


def test_install_bin_pulls_deps_from_registry(
    fhs_root: Path, tmp_path: Path
) -> None:
    pkg = tmp_path / "bin-with-deps"
    pkg.mkdir()
    (pkg / "package.yaml").write_text(
        "name: bin-with-deps\n"
        "kind: bin\n"
        "version: 0.1.0\n"
        "entrypoint: bin_with_deps.py\n"
        "deps:\n"
        "  - testskill1\n"
    )
    (pkg / "bin_with_deps.py").write_text("#!/usr/bin/env python\n")

    assert paiman.main(["install", str(pkg)]) == 0

    assert (fhs_root / "usr" / "bin" / "bin-with-deps").is_file()
    assert (fhs_root / "usr" / "lib" / "skills" / "testskill1").is_symlink()


def test_skill_install_emits_reload(
    fhs_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A skill install reconciles running PAIs by emitting one reload event."""
    from boot import processes as Pr

    calls: list[dict] = []
    monkeypatch.setattr(Pr, "emit_event", lambda payload, *a, **k: calls.append(payload))
    assert paiman.main(["install", str(FIXTURES / "testskill")]) == 0
    assert len(calls) == 1
    assert calls[0]["kind"] == "kernel:reload_config"


def test_no_reload_suppresses_emit(
    fhs_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--no-reload` lets a batch caller (paisetup) install N packages and
    emit a single reload at the end instead of one storm per package."""
    from boot import processes as Pr

    calls: list[dict] = []
    monkeypatch.setattr(Pr, "emit_event", lambda payload, *a, **k: calls.append(payload))
    assert paiman.main(["install", "--no-reload", str(FIXTURES / "testskill")]) == 0
    assert calls == []


def test_reinstall_overwrites(fhs_root: Path, tmp_path: Path) -> None:
    paiman.main(["install", str(FIXTURES / "testskill")])
    bundle = fhs_root / "opt" / "paiman" / "skill" / "testskill"
    # User-edits-in-place: drop a stray file, then reinstall.
    (bundle / "stray.txt").write_text("edit")
    paiman.main(["install", str(FIXTURES / "testskill")])
    assert not (bundle / "stray.txt").exists()
    assert (bundle / "SKILL.md").is_file()


def test_remove(fhs_root: Path) -> None:
    paiman.main(["install", str(FIXTURES / "testskill")])
    assert paiman.main(["remove", "testskill"]) == 0
    assert not (fhs_root / "opt" / "paiman" / "skill" / "testskill").exists()
    assert not (fhs_root / "usr" / "lib" / "skills" / "testskill").exists()


def test_remove_driver_removes_driver_skill(fhs_root: Path) -> None:
    paiman.main(["install", str(FIXTURES / "testdriver")])
    assert paiman.main(["remove", "testdriver"]) == 0
    assert not (fhs_root / "opt" / "paiman" / "driver" / "testdriver").exists()
    assert not (
        fhs_root / "usr" / "lib" / "skills" / "drivers" / "testdriver"
    ).exists()


def test_reinstall_driver_without_skill_removes_old_driver_skill(
    fhs_root: Path, tmp_path: Path
) -> None:
    paiman.main(["install", str(FIXTURES / "testdriver")])

    pkg = tmp_path / "testdriver"
    pkg.mkdir()
    (pkg / "package.yaml").write_text(
        "name: testdriver\nkind: driver\nversion: 0.2.0\n"
    )
    (pkg / "events.yaml").write_text("events: []\n")

    paiman.main(["install", str(pkg)])
    assert not (
        fhs_root / "usr" / "lib" / "skills" / "drivers" / "testdriver"
    ).exists()


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
    assert (fhs_root / "opt" / "paiman" / "skill" / "testskill1").is_dir()
    assert (fhs_root / "usr" / "lib" / "skills" / "testskill1").is_symlink()


def test_install_bare_name_unknown_fails(fhs_root: Path) -> None:
    with pytest.raises(SystemExit, match="not found in registry"):
        paiman.main(["install", "no-such-package"])


def _write_pkg(d: Path, **fields: object) -> None:
    d.mkdir(parents=True, exist_ok=True)
    (d / "package.yaml").write_text(yaml.safe_dump(fields))


def test_lookup_bare_name_collision_prefers_driver_over_bin(
    tmp_path: Path,
) -> None:
    # bin/ax and drivers/ax both exist. A bare `ax` must resolve to the driver
    # (the umbrella that pulls bin/ax and builds the sidecar), not bin/ax which
    # wins on alphabetical order alone.
    reg = tmp_path / "reg"
    _write_pkg(reg / "bin" / "ax", name="ax", kind="bin", entrypoint="ax.py")
    _write_pkg(reg / "drivers" / "ax", name="ax", kind="driver",
               deps=["bin/ax"])
    resolved = paiman._Registry(tmp_path / "work")
    resolved._path = reg.resolve()
    assert resolved.lookup("ax") == (reg / "drivers" / "ax").resolve()


def test_lookup_typed_ref_resolves_nested_skill(tmp_path: Path) -> None:
    # Skills are kind- *and* topic-foldered: skills/<topic>/<name>. A bare name
    # can't resolve that depth, so paisetup installs them by typed ref. The
    # ref's direct form (root/<ref>) must resolve.
    reg = tmp_path / "reg"
    _write_pkg(reg / "skills" / "operating" / "drive-macos-ui",
               name="drive-macos-ui", kind="skill", topic="operating",
               entrypoint="SKILL.md")
    resolved = paiman._Registry(tmp_path / "work")
    resolved._path = reg.resolve()
    assert resolved.lookup("skills/operating/drive-macos-ui") == \
        (reg / "skills" / "operating" / "drive-macos-ui").resolve()


def test_install_subagent_dep_uses_typed_ref_for_same_name(
    fhs_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reg = tmp_path / "registry"
    bin_dir = reg / "bin" / "browse"
    _write_pkg(bin_dir, name="browse", kind="bin", entrypoint="browse.py")
    (bin_dir / "browse.py").write_text("#!/usr/bin/env python\n")

    sub_dir = reg / "subagents" / "browse"
    _write_pkg(
        sub_dir,
        name="browse",
        kind="subagent",
        prompt="prompt.md",
        deps=["bin/browse"],
    )
    (sub_dir / "prompt.md").write_text("browse role\n")

    monkeypatch.setenv("PAIMAN_REGISTRY", str(reg))

    assert paiman.main(["install", "subagents/browse"]) == 0
    assert (fhs_root / "usr" / "lib" / "subagents" / "browse").is_symlink()
    assert (fhs_root / "usr" / "bin" / "browse").is_file()


# ---------- pai install with deps ----------

def test_install_pai_pulls_deps_from_registry(fhs_root: Path) -> None:
    assert paiman.main(["install", str(FIXTURES / "testpai")]) == 0
    # pai bundle activated.
    assert (fhs_root / "usr" / "lib" / "pais" / "testpai").is_symlink()
    # All three deps installed and activated.
    assert (fhs_root / "usr" / "lib" / "skills" / "testskill1").is_symlink()
    assert (fhs_root / "usr" / "bin" / "testbin1").is_file()
    assert (fhs_root / "usr" / "share" / "prompts" / "testprompt1.md").is_symlink()


def test_install_pai_skips_existing_deps(fhs_root: Path) -> None:
    paiman.main(["install", "testskill1"])
    # User adds a file to the installed skill.
    skill_dir = fhs_root / "opt" / "paiman" / "skill" / "testskill1"
    (skill_dir / "user-edit.md").write_text("hand-tweaked")
    paiman.main(["install", str(FIXTURES / "testpai")])
    # Addition must survive — the installed dep matches the registry on every
    # registry-owned file, so paiman skips the reinstall; extra files (hook
    # outputs, user additions) never count as drift.
    assert (skill_dir / "user-edit.md").is_file()


# ---------- dep staleness: exists != current ----------

def _stale_reg(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A mutable tmp registry with bin/tool plus a subagent depending on it.
    Returns (registry_root, tool_src_dir, parent_src_dir)."""
    reg = tmp_path / "stale-registry"
    tool = reg / "bin" / "tool"
    _write_pkg(tool, name="tool", kind="bin", entrypoint="tool.py")
    (tool / "tool.py").write_text("print('v1')\n")
    parent = reg / "subagents" / "parent"
    _write_pkg(parent, name="parent", kind="subagent", prompt="prompt.md",
               deps=["bin/tool"])
    (parent / "prompt.md").write_text("parent role\n")
    return reg, tool, parent


def test_install_dep_stale_copy_reinstalled(
    fhs_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """A registry edit to an already-installed dep reaches the runtime on the
    next install of anything depending on it — the old existence-only check
    left the runtime copy stale forever."""
    reg, tool, _ = _stale_reg(tmp_path)
    monkeypatch.setenv("PAIMAN_REGISTRY", str(reg))
    paiman.main(["install", "bin/tool"])
    installed = fhs_root / "opt" / "paiman" / "bin" / "tool" / "tool.py"
    assert installed.read_text() == "print('v1')\n"

    (tool / "tool.py").write_text("print('v2')\n")
    capsys.readouterr()
    assert paiman.main(["install", "subagents/parent"]) == 0
    out = capsys.readouterr().out
    assert "bin/tool: installed copy stale -> reinstalling" in out
    assert installed.read_text() == "print('v2')\n"
    # Reinstall is audited.
    log = (fhs_root / "var" / "lib" / "paiman" / "log.md").read_text()
    assert "stale dep bin/tool -> reinstall" in log


def test_install_dep_fresh_copy_skipped(
    fhs_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """An installed dep that matches the registry byte-for-byte is left alone —
    including any extra files hooks or the user dropped into the staging dir."""
    reg, _, _ = _stale_reg(tmp_path)
    monkeypatch.setenv("PAIMAN_REGISTRY", str(reg))
    paiman.main(["install", "bin/tool"])
    marker = fhs_root / "opt" / "paiman" / "bin" / "tool" / "hook-output.bin"
    marker.write_text("built at install time")

    capsys.readouterr()
    assert paiman.main(["install", "subagents/parent"]) == 0
    out = capsys.readouterr().out
    assert "stale" not in out
    assert marker.is_file()


def test_install_dep_symlinked_at_registry_skipped(
    fhs_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """A staging dir that IS a symlink to the registry source can't drift —
    the check notes that and skips without re-staging it as a copy."""
    import os as _os

    reg, tool, _ = _stale_reg(tmp_path)
    monkeypatch.setenv("PAIMAN_REGISTRY", str(reg))
    staging = fhs_root / "opt" / "paiman" / "bin" / "tool"
    staging.parent.mkdir(parents=True)
    _os.symlink(tool, staging)

    capsys.readouterr()
    assert paiman.main(["install", "subagents/parent"]) == 0
    out = capsys.readouterr().out
    assert "stale" not in out
    assert staging.is_symlink()  # untouched, not replaced by a copy


def test_install_dep_missing_from_registry_keeps_installed_copy(
    fhs_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """A dep that is installed but no longer resolves in the registry (renamed,
    or registry unreachable) is kept with a warning — staleness checking must
    not break installs that used to succeed."""
    loner = tmp_path / "loner"
    loner.mkdir()
    (loner / "package.yaml").write_text(
        "name: loner\nkind: bin\nversion: 0.1.0\nentrypoint: loner.py\n"
    )
    (loner / "loner.py").write_text("#!/usr/bin/env python\n")
    paiman.main(["install", str(loner)])

    empty_reg = tmp_path / "empty-registry"
    empty_reg.mkdir()
    monkeypatch.setenv("PAIMAN_REGISTRY", str(empty_reg))
    parent = tmp_path / "needs-loner"
    parent.mkdir()
    (parent / "package.yaml").write_text(
        "name: needs-loner\nkind: subagent\nprompt: prompt.md\ndeps: [loner]\n"
    )
    (parent / "prompt.md").write_text("role\n")

    capsys.readouterr()
    assert paiman.main(["install", str(parent)]) == 0
    err = capsys.readouterr().err
    assert "cannot check 'loner'" in err
    assert (fhs_root / "opt" / "paiman" / "bin" / "loner").is_dir()


def test_installed_copy_stale_ignores_copy_ignore_patterns(tmp_path: Path) -> None:
    import shutil as _shutil

    src = tmp_path / "src"
    src.mkdir()
    (src / "package.yaml").write_text("name: x\nkind: bin\nentrypoint: x.py\n")
    (src / "x.py").write_text("code v1\n")
    inst = tmp_path / "inst"
    _shutil.copytree(src, inst)

    # .git / __pycache__ / *.pyc churn in the registry is not drift.
    (src / "__pycache__").mkdir()
    (src / "__pycache__" / "x.cpython-314.pyc").write_bytes(b"\x00")
    (src / "junk.pyc").write_bytes(b"\x00")
    assert paiman._installed_copy_stale(src, inst) is False

    # A registry-owned file edit is.
    (src / "x.py").write_text("code v2\n")
    assert paiman._installed_copy_stale(src, inst) is True

    # Symlink-mode: installed dir resolves to the source itself.
    assert paiman._installed_copy_stale(src, src) is None


def test_install_pai_dep_missing_from_registry_errors(
    fhs_root: Path, tmp_path: Path
) -> None:
    """A pai bundle dep that isn't a registry package is a hard error. paiman
    no longer falls through to pip — registry deps must resolve to bundles, and
    Python deps belong in the kernel venv provisioned by paifs-init, not in an
    ad-hoc per-install pip call."""
    pkg = tmp_path / "needs-pip"
    pkg.mkdir()
    (pkg / "package.yaml").write_text(
        "name: needspip\nkind: pai\ndeps: [some-pypi-pkg]\n"
    )
    with pytest.raises(SystemExit, match="not found in registry"):
        paiman.main(["install", str(pkg)])


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
    assert (fhs_root / "opt" / "paiman" / "skill" / "testskill1").is_dir()


def test_remove_force_overrides_dep_check(fhs_root: Path) -> None:
    paiman.main(["install", str(FIXTURES / "testpai")])
    assert paiman.main(["remove", "--force", "testskill1"]) == 0
    assert not (fhs_root / "opt" / "paiman" / "skill" / "testskill1").exists()


def test_remove_pai_does_not_remove_deps(fhs_root: Path) -> None:
    paiman.main(["install", str(FIXTURES / "testpai")])
    paiman.main(["remove", "testpai"])
    # Pai gone; primitives stay.
    assert not (fhs_root / "opt" / "paiman" / "pai" / "testpai").exists()
    assert (fhs_root / "opt" / "paiman" / "skill" / "testskill1").is_dir()


def test_list_installed(fhs_root: Path, capsys: pytest.CaptureFixture) -> None:
    paiman.main(["install", str(FIXTURES / "testskill")])
    paiman.main(["install", str(FIXTURES / "testbin")])
    capsys.readouterr()
    paiman.main(["list"])
    out = capsys.readouterr().out
    assert "testskill" in out
    assert "testbin" in out


# ---------- git-less registry: GitHub URL -> codeload tarball ----------

@pytest.mark.parametrize(
    "loc,expected",
    [
        (
            "https://github.com/whitematterlabs/pairegistry",
            "https://github.com/whitematterlabs/pairegistry/archive/refs/heads/main.tar.gz",
        ),
        (
            "https://github.com/whitematterlabs/pairegistry.git",
            "https://github.com/whitematterlabs/pairegistry/archive/refs/heads/main.tar.gz",
        ),
        (
            "https://github.com/whitematterlabs/pairegistry@dev",
            "https://github.com/whitematterlabs/pairegistry/archive/refs/heads/dev.tar.gz",
        ),
        (
            "github.com/owner/repo",
            "https://github.com/owner/repo/archive/refs/heads/main.tar.gz",
        ),
    ],
)
def test_github_tarball_url_derivation(loc: str, expected: str) -> None:
    assert paiman._github_tarball_url(loc) == expected


@pytest.mark.parametrize(
    "loc",
    [
        "https://gitlab.com/owner/repo",
        "https://example.com/owner/repo",
        "https://github.com/onlyowner",
    ],
)
def test_github_tarball_url_rejects_non_github(loc: str) -> None:
    assert paiman._github_tarball_url(loc) is None
