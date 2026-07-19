from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pytest
import yaml

from bin import paiadd, paiman
from boot import config as C
from boot import paths
from boot import processes as P


@pytest.fixture
def fhs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "pai"
    for sub in (
        "etc",
        "usr/lib/pais",
        "var/lib/instances",
        "var/lib/memory/people",
        "var/lib/memory/topics",
        "usr/lib/skills",
        "home",
        "root",
    ):
        (root / sub).mkdir(parents=True)
    events = root / "run" / "pai" / "events"
    events.mkdir(parents=True)
    monkeypatch.setattr(paths, "PAI_ROOT", root, raising=True)
    monkeypatch.setattr(C, "CONFIG_PATH", root / "etc" / "config.yaml", raising=True)
    monkeypatch.setattr(C, "PACKAGES_DIR", root / "usr" / "lib" / "pais", raising=True)
    monkeypatch.setattr(P, "EVENTS_DIR", events, raising=True)
    paiman.main(["init", "email-pai"])
    return root


def _scripted_input(answers: list[str]) -> Iterator[str]:
    yield from answers


def _patch_input(monkeypatch: pytest.MonkeyPatch, answers: list[str]) -> None:
    it = iter(answers)
    monkeypatch.setattr("builtins.input", lambda prompt="": next(it))


def test_add_creates_instance_and_config(fhs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_input(
        monkeypatch,
        [
            "",                # instance name → default 'email-pai'
            "handles email",   # description
            "",                # provider → default
            "",                # model → blank
            "gmail:*,imap:*",  # wake_on
            "n",               # fallback
            "y",               # proceed
        ],
    )
    assert paiadd.main(["email-pai"]) == 0

    instance = fhs / "var" / "lib" / "instances" / "email-pai"
    assert (instance / "memory" / "private").is_dir()
    assert (instance / "inbox").is_dir()

    home = fhs / "home" / "email-pai"
    # Shared memory is un-nested: each top-level entry of var/lib/memory
    # links directly under memory/; the legacy memory/shared link is gone.
    assert (home / "memory" / "people").is_symlink()
    assert (home / "memory" / "topics").is_symlink()
    assert not (home / "memory" / "shared").exists()
    # memory/skills is now a directory of per-skill symlinks (filtered by
    # `visible_to:`), not a single symlink to /usr/lib/skills/.
    assert (home / "memory" / "skills").is_dir()
    assert not (home / "memory" / "skills").is_symlink()

    cfg = yaml.safe_load((fhs / "etc" / "config.yaml").read_text())
    entry = next(e for e in cfg["pais"] if e["name"] == "email-pai")
    assert entry["package"] == "email-pai"
    assert entry["description"] == "handles email"
    assert entry["wake_on"] == ["gmail:*", "imap:*"]
    assert "fallback" not in entry  # default false → omitted

    events = list((fhs / "run" / "pai" / "events").iterdir())
    assert len(events) == 1
    payload = yaml.safe_load(events[0].read_text())
    assert payload == {"kind": "kernel:reload_config", "source": "paiadd", "added": "email-pai"}


def test_add_refuses_duplicate(fhs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_input(monkeypatch, ["", "first", "", "", "", "n", "y"])
    paiadd.main(["email-pai"])

    _patch_input(monkeypatch, ["", "second"])
    with pytest.raises(SystemExit, match="already in"):
        paiadd.main(["email-pai"])


def test_add_refuses_missing_bundle(fhs: Path) -> None:
    with pytest.raises(SystemExit, match="not found"):
        paiadd.main(["does-not-exist"])


def test_add_aborts_on_decline(fhs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_input(monkeypatch, ["", "desc", "", "", "", "n", "n"])
    assert paiadd.main(["email-pai"]) == 1
    assert not (fhs / "var" / "lib" / "instances" / "email-pai").exists()
    assert not (fhs / "etc" / "config.yaml").exists()
