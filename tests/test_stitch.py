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
        "usr/lib/skills",
        "usr/share/doc",
    ):
        (root / sub).mkdir(parents=True)
    monkeypatch.setattr(paths, "PAI_ROOT", root, raising=True)
    monkeypatch.setattr(C, "CONFIG_PATH", root / "etc" / "config.yaml", raising=True)
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
