from __future__ import annotations

from pathlib import Path

import yaml

from bin import resolve_contact


def _write_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        yaml.safe_dump(data, f, sort_keys=False)


def test_resolve_contact_preserves_routing_metadata(
    tmp_path: Path,
    monkeypatch,
) -> None:
    people = tmp_path / "var" / "lib" / "memory" / "people"
    messages = tmp_path / "var" / "spool" / "communication" / "messages"
    old = "17147853574"
    new = "alper-yilmaz"

    old_person = people / old
    old_thread = messages / old
    _write_yaml(
        old_person / "about.yaml",
        {
            "name": old,
            "handles": ["+17147853574"],
            "relationship": "",
            "entry": "",
        },
    )
    _write_yaml(
        old_thread / "meta.yaml",
        {
            "description": "",
            "created": "2026-05-24",
            "group": False,
            "handles": ["+17147853574"],
            "channel": "imessage",
        },
    )
    (old_thread / old).symlink_to(
        Path("..") / ".." / ".." / ".." / "lib" / "memory" / "people" / old
    )

    monkeypatch.setattr(resolve_contact, "PEOPLE_DIR", people, raising=True)
    monkeypatch.setattr(resolve_contact, "MESSAGES_DIR", messages, raising=True)

    assert resolve_contact.main([old, "Alper Yilmaz"]) == 0

    assert not old_person.exists()
    assert not old_thread.exists()

    about = yaml.safe_load((people / new / "about.yaml").read_text())
    assert about["name"] == "Alper Yilmaz"
    assert about["handles"] == ["+17147853574"]

    meta_path = messages / new / "meta.yaml"
    assert meta_path.exists()
    meta = yaml.safe_load(meta_path.read_text())
    assert meta["handles"] == ["+17147853574"]
    assert meta["channel"] == "imessage"
    assert meta["display_name"] == "Alper Yilmaz"

    link = messages / new / new
    assert link.is_symlink()
    assert link.readlink() == (
        Path("..") / ".." / ".." / ".." / "lib" / "memory" / "people" / new
    )
