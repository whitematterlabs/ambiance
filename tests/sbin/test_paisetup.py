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


def test_picker_shows_only_visible_driver_choices() -> None:
    # Only drivers are surfaced; PAI bundles and subagents never render, and
    # force-installed drivers (calendar) are hidden too.
    rows = picker._build_rows({
        "driver": [
            _item(kind="driver", name="whatsapp"),
            _item(kind="driver", name="calendar"),  # AUTO_INSTALL -> hidden
        ],
        "skill": [_item(kind="skill", name="drive-macos-ui")],
        "pai": [_item(kind="pai", name="calendar-agent")],
        "subagent": [_item(kind="subagent", name="browse")],  # AUTO_INSTALL -> hidden
    })

    assert [r.kind for r in rows if r.is_header] == ["driver"]
    assert [
        (r.kind, r.item.name)
        for r in rows
        if not r.is_header and r.item is not None
    ] == [("driver", "whatsapp")]


def test_auto_install_items_are_hidden() -> None:
    assert picker.is_hidden("subagent", "browse")
    assert picker.is_hidden("subagent", "computer-use")
    assert picker.is_hidden("driver", "calendar")
    assert not picker.is_hidden("driver", "whatsapp")


def test_visible_drivers_checked_by_default() -> None:
    rows = picker._build_rows({
        "driver": [_item(kind="driver", name="whatsapp")],
    })
    states = {
        (r.kind, r.item.name): r.checked
        for r in rows
        if not r.is_header and r.item is not None
    }
    assert states == {("driver", "whatsapp"): True}


def test_json_catalog_shows_only_visible_drivers(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        paisetup_app,
        "discover",
        lambda: {
            "driver": [
                _item(kind="driver", name="whatsapp", ref="drivers/whatsapp"),
                _item(kind="driver", name="calendar", ref="drivers/calendar"),  # hidden
            ],
            "skill": [
                _item(
                    kind="skill",
                    name="drive-macos-ui",
                    ref="skills/operating/drive-macos-ui",
                )
            ],
            "subagent": [
                _item(kind="subagent", name="browse", ref="subagents/browse"),  # hidden
            ],
        },
    )

    assert paisetup_app._emit_catalog_json() == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["auto_checked"] == ["driver"]
    assert payload["auto_checked_refs"] == [
        "drivers/ax",
        "drivers/calendar",
        "drivers/imessage",
        "drivers/notification",
        "subagents/browse",
        "subagents/computer-use",
    ]
    assert set(payload["groups"]) == {"driver"}
    defaults = {
        (kind, item["name"]): item["default_checked"]
        for kind, items in payload["groups"].items()
        for item in items
    }
    assert defaults == {("driver", "whatsapp"): True}


def test_auto_install_items_merged_into_install(monkeypatch) -> None:
    """Hidden force-install items install alongside the owner's picks, even when
    not chosen; ones absent from the registry are skipped silently."""
    groups = {
        "driver": [
            _item(kind="driver", name="calendar", ref="drivers/calendar"),
            _item(kind="driver", name="whatsapp", ref="drivers/whatsapp"),
        ],
        "subagent": [
            _item(kind="subagent", name="browse", ref="subagents/browse"),
            _item(kind="subagent", name="computer-use", ref="subagents/computer-use"),
        ],
        "skill": [],
        "pai": [],
    }
    monkeypatch.setattr(paisetup_app, "_tty_available", lambda: True)
    monkeypatch.setattr(paisetup_app, "discover", lambda: groups)
    # Owner picks only whatsapp from the visible drivers.
    monkeypatch.setattr(paisetup_app.picker, "run", lambda g: {"driver": ["whatsapp"]})
    monkeypatch.setattr(paisetup_app, "_install_arg", lambda it: it.name)

    installed: list[str] = []
    monkeypatch.setattr(
        paisetup_app.paiman, "main",
        lambda argv: (installed.append(argv[-1]), 0)[1],
    )
    import boot.processes as _proc
    monkeypatch.setattr(_proc, "emit_event", lambda e: None)

    assert paisetup_app.main([]) == 0
    # Chosen whatsapp + hidden auto-install items present in the registry.
    assert set(installed) == {"whatsapp", "calendar", "browse", "computer-use"}
    # ax/imessage/notification weren't in the registry groups -> skipped.


# --- API-key dialogue --------------------------------------------------------

from sbin.paisetup import apikey  # noqa: E402


def _seed_config(root: Path, provider: str = "deepseek") -> None:
    (root / "etc").mkdir(parents=True, exist_ok=True)
    (root / "etc" / "config.yaml").write_text(
        f"pais:\n  - name: pai\n    provider: {provider}\n    model: x\n"
    )


def test_ensure_api_key_reads_seeded_provider(tmp_path: Path) -> None:
    _seed_config(tmp_path, "openai")
    assert apikey._seeded_provider(tmp_path) == "openai"


def test_ensure_api_key_skips_when_in_env(tmp_path: Path, monkeypatch, capsys) -> None:
    _seed_config(tmp_path, "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-live")
    apikey.ensure_api_key(tmp_path)
    assert "found in environment" in capsys.readouterr().out
    assert not (tmp_path / ".env").exists()  # never written


def test_ensure_api_key_skips_when_in_env_file(tmp_path: Path, monkeypatch, capsys) -> None:
    _seed_config(tmp_path, "deepseek")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    (tmp_path / ".env").write_text("DEEPSEEK_API_KEY=sk-fromfile\n")
    apikey.ensure_api_key(tmp_path)
    out = capsys.readouterr().out
    assert ".env" in out and "found" in out
    # File left untouched (no duplicate appended).
    assert (tmp_path / ".env").read_text() == "DEEPSEEK_API_KEY=sk-fromfile\n"


def test_ensure_api_key_prompts_and_writes(tmp_path: Path, monkeypatch) -> None:
    _seed_config(tmp_path, "deepseek")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(apikey.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(apikey.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(apikey.getpass, "getpass", lambda prompt="": "sk-typed")
    apikey.ensure_api_key(tmp_path)
    env = tmp_path / ".env"
    assert env.read_text() == "DEEPSEEK_API_KEY=sk-typed\n"
    assert (env.stat().st_mode & 0o777) == 0o600


def test_ensure_api_key_blank_input_skips_write(tmp_path: Path, monkeypatch) -> None:
    _seed_config(tmp_path, "deepseek")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(apikey.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(apikey.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(apikey.getpass, "getpass", lambda prompt="": "   ")
    apikey.ensure_api_key(tmp_path)
    assert not (tmp_path / ".env").exists()


def test_ensure_api_key_noninteractive_warns_no_write(tmp_path: Path, monkeypatch, capsys) -> None:
    _seed_config(tmp_path, "deepseek")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(apikey.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(apikey.sys.stdout, "isatty", lambda: False)
    apikey.ensure_api_key(tmp_path)
    assert "DEEPSEEK_API_KEY not set" in capsys.readouterr().err
    assert not (tmp_path / ".env").exists()
