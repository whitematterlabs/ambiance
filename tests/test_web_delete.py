from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from boot import config as C
from boot import paths
from boot import processes as P
from boot import stitch
from usr.libexec.web.pai_web import actions


@pytest.fixture
def fhs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "pai"
    for sub in (
        "etc",
        "home",
        "root",
        "proc",
        "run/pai/events",
        "run/pais",
        "var/lib/instances",
    ):
        (root / sub).mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(paths, "PAI_ROOT", root, raising=True)
    monkeypatch.setattr(C, "CONFIG_PATH", root / "etc" / "config.yaml", raising=True)
    monkeypatch.setattr(P, "EVENTS_DIR", root / "run" / "pai" / "events", raising=True)
    return root


def _write_config(root: Path, pais: list[dict]) -> None:
    (root / "etc" / "config.yaml").write_text(
        yaml.safe_dump({"pais": pais}, sort_keys=False), encoding="utf-8"
    )


def _config_names(root: Path) -> list[str]:
    cfg = yaml.safe_load((root / "etc" / "config.yaml").read_text(encoding="utf-8"))
    return [e["name"] for e in cfg["pais"]]


def test_clone_of_helper_reads_marker(fhs: Path) -> None:
    _write_config(
        fhs,
        [
            {"name": "helper", "pid": 7},
            {"name": "helper-2", "clone_of": "helper"},
        ],
    )
    assert C.clone_of("helper") is None
    assert C.clone_of("helper-2") == "helper"
    assert C.clone_of("absent") is None


def test_delete_pai_refuses_original(fhs: Path) -> None:
    _write_config(fhs, [{"name": "helper", "pid": 7}])

    with pytest.raises(ValueError, match="not a clone"):
        actions.delete_pai("helper")

    # Defense-in-depth refusal must not mutate the fleet.
    assert _config_names(fhs) == ["helper"]
    assert not list((fhs / "run" / "pai" / "events").iterdir())


def test_delete_pai_refuses_unknown(fhs: Path) -> None:
    _write_config(fhs, [])

    with pytest.raises(ValueError, match="not a clone"):
        actions.delete_pai("ghost")


def test_delete_pai_purges_clone(fhs: Path) -> None:
    _write_config(
        fhs,
        [
            {"name": "helper", "pid": 7},
            {"name": "helper-2", "clone_of": "helper"},
        ],
    )
    # Stage runtime state as if the kernel had already drained + stopped it, so
    # the stop-then-purge wait loop exits immediately (no live kernel in tests).
    proc_dir = paths.proc("helper-2")
    proc_dir.mkdir(parents=True, exist_ok=True)
    (proc_dir / "status").write_text("stopped\n", encoding="utf-8")
    instance = paths.var_lib_instance("helper-2")
    (instance / "memory").mkdir(parents=True, exist_ok=True)
    home = stitch.home_for("helper-2")
    home.mkdir(parents=True, exist_ok=True)
    run_dir = paths.run_pais("helper-2")
    run_dir.mkdir(parents=True, exist_ok=True)

    result = actions.delete_pai("helper-2")

    assert result["name"] == "helper-2"
    assert result["purged"] is True
    assert not proc_dir.exists()
    assert not instance.exists()
    assert not home.exists()
    assert not run_dir.exists()
    assert _config_names(fhs) == ["helper"]


def test_delete_pai_rejects_blank(fhs: Path) -> None:
    _write_config(fhs, [])
    with pytest.raises(ValueError, match="missing PAI name"):
        actions.delete_pai("   ")
