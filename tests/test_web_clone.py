from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from bin import paiclone
from boot import config as C
from boot import paths
from boot import processes as P
from usr.libexec.web.pai_web import actions


@pytest.fixture
def fhs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "pai"
    for sub in (
        "etc",
        "home",
        "root",
        "run/pai/events",
        "usr/lib/pais",
        "usr/lib/skills",
        "usr/share/doc",
        "var/lib/instances",
        "var/lib/memory",
    ):
        (root / sub).mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(paths, "PAI_ROOT", root, raising=True)
    monkeypatch.setattr(C, "CONFIG_PATH", root / "etc" / "config.yaml", raising=True)
    monkeypatch.setattr(P, "EVENTS_DIR", root / "run" / "pai" / "events", raising=True)
    return root


def test_clone_pai_uses_shared_paiclone_flow(fhs: Path) -> None:
    (fhs / "etc" / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "pais": [
                    {
                        "name": "helper",
                        "pid": 7,
                        "package": "helper",
                        "description": "handles delegated work",
                        "provider": "anthropic",
                        "wake_on": ["delegation:*"],
                        "heartbeat": "1h",
                    }
                ]
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    result = actions.clone_pai("helper")

    assert result["source"] == "helper"
    assert result["name"] in paiclone._CLONE_NAMES
    assert result["name"] != "helper"  # should be a different name
    clone_name = result["name"]
    assert Path(result["instance"]) == fhs / "var" / "lib" / "instances" / clone_name
    assert Path(result["home"]) == fhs / "home" / clone_name
    assert (fhs / "var" / "lib" / "instances" / clone_name / "memory" / "private").is_dir()

    config = yaml.safe_load((fhs / "etc" / "config.yaml").read_text(encoding="utf-8"))
    clone = next(e for e in config["pais"] if e["name"] == clone_name)
    assert clone["package"] == "helper"
    assert clone["description"] == "handles delegated work"
    # Clones do NOT inherit wakes — they start inert so N identical catch-alls
    # can't all fire on every event (B1 load-amplification trap).
    assert "wake_on" not in clone
    # Nor the idle heartbeat — autonomous wake behavior is spend, same as
    # routing; a cloned beat would silently double the LLM bill.
    assert "heartbeat" not in clone
    assert "pid" not in clone
    # Behavior-free provenance marker stamped at clone time — gates deletion.
    assert clone["clone_of"] == "helper"

    events = list((fhs / "run" / "pai" / "events").iterdir())
    assert len(events) == 1
    payload = yaml.safe_load(events[0].read_text(encoding="utf-8"))
    assert payload == {"kind": "kernel:reload_config", "source": "paiadd", "added": clone_name}


def test_clone_pai_rejects_unknown_source(fhs: Path) -> None:
    (fhs / "etc" / "config.yaml").write_text("pais: []\n", encoding="utf-8")

    with pytest.raises(ValueError, match="no PAI named"):
        actions.clone_pai("missing")
