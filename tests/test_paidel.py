from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from bin import paiadd, paidel, paiman
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
        "var/lib/memory",
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
    # Stand up an instance via the wizard.
    answers = iter(["", "handles email", "", "", "gmail:*", "n", "y"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    paiadd.main(["email-pai"])
    return root


def test_del_preserves_instance(fhs: Path) -> None:
    assert paidel.main(["email-pai"]) == 0
    assert not (fhs / "home" / "email-pai").exists()
    # Instance state survives.
    assert (fhs / "var" / "lib" / "instances" / "email-pai" / "memory" / "private").is_dir()
    cfg = yaml.safe_load((fhs / "etc" / "config.yaml").read_text())
    assert all(e["name"] != "email-pai" for e in cfg.get("pais", []))


def test_del_purge_wipes_instance(fhs: Path) -> None:
    assert paidel.main(["email-pai", "--purge"]) == 0
    assert not (fhs / "home" / "email-pai").exists()
    assert not (fhs / "var" / "lib" / "instances" / "email-pai").exists()


def test_del_emits_reload_event(fhs: Path) -> None:
    events_dir = fhs / "run" / "pai" / "events"
    # paiadd already emitted one; clear before paidel.
    for f in events_dir.iterdir():
        f.unlink()
    paidel.main(["email-pai"])
    events = list(events_dir.iterdir())
    assert len(events) == 1
    payload = yaml.safe_load(events[0].read_text())
    assert payload == {"kind": "kernel:reload_config", "source": "paidel", "removed": "email-pai"}


def test_del_refuses_unknown(fhs: Path) -> None:
    with pytest.raises(SystemExit, match="not found"):
        paidel.main(["does-not-exist"])


def test_del_purge_only_when_orphan_instance(fhs: Path) -> None:
    """If the home/config are gone but instance state lingers, --purge cleans it."""
    paidel.main(["email-pai"])
    assert (fhs / "var" / "lib" / "instances" / "email-pai").is_dir()
    # Now purge the orphan.
    assert paidel.main(["email-pai", "--purge"]) == 0
    assert not (fhs / "var" / "lib" / "instances" / "email-pai").exists()


def test_del_cleans_proc_and_run(fhs: Path) -> None:
    proc_dir = fhs / "proc" / "email-pai"
    run_dir = fhs / "run" / "pais" / "email-pai"
    proc_dir.mkdir(parents=True)
    (proc_dir / "spec.yaml").write_text("kind: pai\n")
    (proc_dir / "status").write_text("stopped\n")
    run_dir.mkdir(parents=True)
    (run_dir / "pid").write_text("12345\n")

    paidel.main(["email-pai"])
    assert not proc_dir.exists()
    assert not run_dir.exists()


def test_del_refuses_when_running(fhs: Path) -> None:
    proc_dir = fhs / "proc" / "email-pai"
    proc_dir.mkdir(parents=True)
    (proc_dir / "status").write_text("running\n")

    with pytest.raises(SystemExit, match="paictl stop"):
        paidel.main(["email-pai"])
    # Nothing should have been mutated.
    assert proc_dir.exists()
    assert (fhs / "home" / "email-pai").exists()
    cfg = yaml.safe_load((fhs / "etc" / "config.yaml").read_text())
    assert any(e["name"] == "email-pai" for e in cfg["pais"])
