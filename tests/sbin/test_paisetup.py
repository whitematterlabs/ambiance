"""paisetup install-arg selection.

Regression for: `paisetup: failed: drive-macos-ui`. The picker hands back bare
package names; the install loop must turn each one into something
`paiman install` can resolve. For a URL-cloned registry the discovered source
path points into a TemporaryDirectory that discover() has already deleted, so
the loop must fall back to the *typed ref* (`skills/<topic>/<name>`) rather than
the bare name — paiman's bare-name lookup only resolves one-level kinds like
`drivers/<name>`, never topic-nested skills.
"""

from __future__ import annotations

from pathlib import Path

from sbin.paisetup.app import _install_arg
from sbin.paisetup.inventory import Item


def _item(**over: object) -> Item:
    base = dict(kind="skill", name="drive-macos-ui", description="",
                installed=False, source="", ref="")
    base.update(over)
    return Item(**base)  # type: ignore[arg-type]


def test_install_arg_prefers_live_source(tmp_path: Path) -> None:
    src = tmp_path / "skills" / "operating" / "drive-macos-ui"
    src.mkdir(parents=True)
    it = _item(source=str(src), ref="skills/operating/drive-macos-ui")
    assert _install_arg(it) == str(src)


def test_install_arg_falls_back_to_ref_when_source_dead(tmp_path: Path) -> None:
    # Source points into a tempdir that's already been cleaned up.
    dead = tmp_path / "gone" / "drive-macos-ui"  # never created
    it = _item(source=str(dead), ref="skills/operating/drive-macos-ui")
    assert _install_arg(it) == "skills/operating/drive-macos-ui"


def test_install_arg_bare_name_last_resort() -> None:
    it = _item(name="x", source="", ref="")
    assert _install_arg(it) == "x"
