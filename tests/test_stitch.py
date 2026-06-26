"""Regression tests for `boot.stitch`.

The bug being pinned: a driver declares `home.links: communication/email →
var/spool/communication/email`, but the PAI's `communication` is itself a
symlink to `var/spool/communication`. Stitched naively, the new link lands
physically at `var/spool/communication/email` and points at itself →
ELOOP on the next `mkdir(parents=True)`.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from boot import config as C
from boot import paths, stitch
from boot import processes as P


@pytest.fixture
def fhs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "pai"
    for sub in (
        "etc",
        "home",
        "var/lib/instances",
        "var/lib/memory",
        "var/spool/communication",
        "usr/lib/drivers",
        "usr/lib/pais",
        "usr/lib/subagents",
        "usr/lib/skills",
        "usr/share/doc",
        "proc",
    ):
        (root / sub).mkdir(parents=True)
    monkeypatch.setattr(paths, "PAI_ROOT", root, raising=True)
    monkeypatch.setattr(C, "CONFIG_PATH", root / "etc" / "config.yaml", raising=True)
    monkeypatch.setattr(P, "PROC_DIR", root / "proc", raising=True)
    # Bundleless `pai`: empty `pais:` in config so `package_for("pai")` → None.
    (root / "etc" / "config.yaml").write_text(yaml.safe_dump({"pais": []}))
    return root


def test_stitches_simple_communication_link_for_bundleless_pai(fhs: Path) -> None:
    stitch.stitch_home("pai")
    comm = fhs / "home" / "pai" / "communication"
    assert comm.is_symlink()
    assert comm.resolve() == (fhs / "var" / "spool" / "communication").resolve()


def _install_email_driver(fhs: Path) -> None:
    pkg = fhs / "usr" / "lib" / "drivers" / "email"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "package.yaml").write_text(
        yaml.safe_dump(
            {
                "home": {
                    "links": [
                        {
                            "link": "communication/email",
                            "target": "var/spool/communication/email",
                        }
                    ]
                }
            }
        )
    )


def test_does_not_create_self_symlink_when_driver_nests_under_seeded_dir(
    fhs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_email_driver(fhs)
    # Force the bundleless `pai` to mount the `email` driver — bundleless +
    # non-fallback would normally mount no drivers.
    monkeypatch.setattr(stitch, "mounted_drivers_for", lambda slug: {"email"})

    stitch.stitch_home("pai")

    email_path = fhs / "var" / "spool" / "communication" / "email"
    if email_path.is_symlink():
        # If a symlink exists, it must not loop back to itself.
        target = os.readlink(email_path)
        resolved_target = (email_path.parent / target).resolve()
        assert resolved_target != email_path.resolve(strict=False), (
            f"self-symlink at {email_path} → {target}"
        )

    # The actual symptom that crashed macmail-out: creating drafts/ underneath.
    drafts = email_path / "drafts"
    drafts.mkdir(parents=True, exist_ok=True)
    assert drafts.is_dir()


def _write_skill(skill_dir: Path, name: str, body: str = "do the thing") -> None:
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {name} skill\n---\n\n{body}\n"
    )


def _skills_view(fhs: Path, slug: str) -> Path:
    home = stitch.home_for(slug) if slug == "root" else fhs / "home" / slug
    return home / "memory" / "skills"


def test_writable_skill_overlay_private_and_shared(fhs: Path) -> None:
    # Shared (fleet-wide) skill + a private skill only `pai` owns.
    _write_skill(paths.var_lib_skills() / "shared-flow", "shared-flow")
    _write_skill(paths.var_lib_instance_skills("pai") / "private-flow", "private-flow")

    stitch.stitch_home("pai")
    stitch.stitch_home("root")

    pai_skills = _skills_view(fhs, "pai")
    assert (pai_skills / "shared-flow" / "SKILL.md").exists()
    assert (pai_skills / "private-flow" / "SKILL.md").exists()

    # A different PAI sees the shared skill but not pai's private one.
    root_skills = _skills_view(fhs, "root")
    assert (root_skills / "shared-flow" / "SKILL.md").exists()
    assert not (root_skills / "private-flow").exists()


def test_overlay_overrides_baseline_of_same_name(fhs: Path) -> None:
    # Baseline (read-only) flat skill, then an overlay with the same name.
    _write_skill(paths.usr_lib_skills() / "deploy", "deploy", body="BASELINE")
    _write_skill(paths.var_lib_skills() / "deploy", "deploy", body="OVERLAY")

    stitch.stitch_home("pai")

    link = _skills_view(fhs, "pai") / "deploy"
    assert link.is_symlink()
    # Overlay wins: the link resolves into var/lib/skills, not usr/lib/skills.
    assert (link / "SKILL.md").read_text().strip().endswith("OVERLAY")


def test_overlay_survives_restitch(fhs: Path) -> None:
    _write_skill(paths.var_lib_skills() / "shared-flow", "shared-flow")
    _write_skill(paths.var_lib_instance_skills("pai") / "private-flow", "private-flow")

    stitch.stitch_home("pai")
    stitch.stitch_home("pai")  # re-stitch

    pai_skills = _skills_view(fhs, "pai")
    # Real source files untouched; links rebuilt and still present.
    assert (pai_skills / "shared-flow" / "SKILL.md").exists()
    assert (pai_skills / "private-flow" / "SKILL.md").exists()
    assert paths.var_lib_skills().joinpath("shared-flow", "SKILL.md").exists()


def test_seed_instance_creates_skills_dir(fhs: Path) -> None:
    stitch.stitch_home("pai")
    assert paths.var_lib_instance_skills("pai").is_dir()


def test_subagent_package_deps_mount_prefixed_driver(fhs: Path) -> None:
    _install_email_driver(fhs)
    pkg = fhs / "usr" / "lib" / "subagents" / "computer-use"
    pkg.mkdir(parents=True)
    (pkg / "package.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "computer-use",
                "kind": "subagent",
                "deps": ["drivers/email"],
            }
        )
    )
    P.spawn_pai(
        pid=5,
        slug="pai.computer-use",
        description="macOS UI operator",
        parent=2,
        extra={"persistent": True, "persub": True, "package": "computer-use"},
    )

    assert stitch.mounted_drivers_for("pai.computer-use") == {"email"}
