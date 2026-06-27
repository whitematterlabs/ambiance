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

import json
from pathlib import Path

from sbin.paisetup import app as paisetup_app
from sbin.paisetup import picker
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


def test_picker_hides_skills_section() -> None:
    rows = picker._build_rows({
        "driver": [_item(kind="driver", name="calendar")],
        "skill": [_item(kind="skill", name="drive-macos-ui")],
        "pai": [_item(kind="pai", name="calendar-agent")],
        "subagent": [_item(kind="subagent", name="browse")],
    })

    assert [r.kind for r in rows if r.is_header] == ["driver", "pai", "subagent"]
    assert [
        (r.kind, r.item.name)
        for r in rows
        if not r.is_header and r.item is not None
    ] == [
        ("driver", "calendar"),
        ("pai", "calendar-agent"),
        ("subagent", "browse"),
    ]


def test_picker_auto_checks_drivers_browse_computer_use_and_scout() -> None:
    rows = picker._build_rows({
        "driver": [_item(kind="driver", name="calendar")],
        "subagent": [
            _item(kind="subagent", name="browse"),
            _item(kind="subagent", name="computer-use"),
            _item(kind="subagent", name="scout"),
        ],
    })

    states = {
        (r.kind, r.item.name): r.checked
        for r in rows
        if not r.is_header and r.item is not None
    }
    assert states == {
        ("driver", "calendar"): True,
        ("subagent", "browse"): True,
        ("subagent", "computer-use"): True,
        ("subagent", "scout"): True,
    }


def test_json_catalog_hides_skills_and_marks_default_checked(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        paisetup_app,
        "discover",
        lambda: {
            "driver": [_item(kind="driver", name="calendar", ref="drivers/calendar")],
            "skill": [
                _item(
                    kind="skill",
                    name="drive-macos-ui",
                    ref="skills/operating/drive-macos-ui",
                )
            ],
            "subagent": [
                _item(kind="subagent", name="browse", ref="subagents/browse"),
                _item(kind="subagent", name="computer-use", ref="subagents/computer-use"),
                _item(kind="subagent", name="scout", ref="subagents/scout"),
            ],
        },
    )

    assert paisetup_app._emit_catalog_json() == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["auto_checked"] == ["driver"]
    assert payload["auto_checked_refs"] == [
        "subagents/browse",
        "subagents/computer-use",
        "subagents/scout",
    ]
    assert set(payload["groups"]) == {"driver", "subagent"}
    defaults = {
        (kind, item["name"]): item["default_checked"]
        for kind, items in payload["groups"].items()
        for item in items
    }
    assert defaults == {
        ("driver", "calendar"): True,
        ("subagent", "browse"): True,
        ("subagent", "computer-use"): True,
        ("subagent", "scout"): True,
    }
