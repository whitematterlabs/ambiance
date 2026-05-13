"""Boot-time event-storm backfill phase."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from boot import paths
from boot import processes as P
from boot.phases import backfill


def _spawn(slug: str, pid: int, *, fallback: bool = False) -> None:
    P.spawn_pai(pid=pid, slug=slug, description=f"{slug} test", fallback=fallback)


def _write_event(events_dir: Path, name: str, payload: dict) -> Path:
    path = events_dir / name
    path.write_text(yaml.safe_dump(payload, sort_keys=False))
    return path


@pytest.fixture
def backfill_env(live_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """live_dir already sets EVENTS_DIR on processes; mirror it on paths
    and redirect var/log too so emit_event + archive both land in tmp."""
    events = live_dir / "events"
    var_log = tmp_path / "var" / "log"
    var_log.mkdir(parents=True)
    monkeypatch.setattr(paths, "EVENTS_DIR", events, raising=True)
    monkeypatch.setattr(paths, "var_log", lambda: var_log, raising=True)
    return events, var_log


def test_storm_collapses_into_single_backfill(backfill_env, live_dir) -> None:
    events, var_log = backfill_env
    _spawn("pai", pid=2, fallback=True)

    for i in range(50):
        # Half new_message, half new_email-shaped — mixed kinds in by_kind.
        if i % 2 == 0:
            _write_event(events, f"20260101T00{i:04d}-imessage.yaml", {
                "source": "imessage", "kind": "new_message",
                "handle": "+1", "text": f"msg {i}",
            })
        else:
            _write_event(events, f"20260101T00{i:04d}-email.yaml", {
                "source": "email", "kind": "new",
                "from": "x@y.z", "subject": f"sub {i}",
            })

    backfill.run()

    remaining = sorted(p.name for p in events.iterdir() if p.suffix == ".yaml")
    # Exactly one event left: the synthetic backfill.
    assert len(remaining) == 1
    synthetic_path = events / remaining[0]
    synthetic = yaml.safe_load(synthetic_path.read_text())
    assert synthetic["kind"] == "kernel:backfill"
    assert synthetic["target_pid"] == 2
    assert synthetic["count"] == 50
    assert synthetic["by_kind"] == {"new_message": 25, "new": 25}
    assert "window" in synthetic and "from" in synthetic["window"]

    archive_root = var_log / "events" / "backfill"
    boot_dirs = list(archive_root.iterdir())
    assert len(boot_dirs) == 1
    archived = list((boot_dirs[0] / "pid-2").glob("*.yaml"))
    assert len(archived) == 50


def test_below_threshold_passes_through(backfill_env, live_dir) -> None:
    events, var_log = backfill_env
    _spawn("pai", pid=2, fallback=True)

    for i in range(5):
        _write_event(events, f"20260101T00{i:04d}-imessage.yaml", {
            "source": "imessage", "kind": "new_message",
            "handle": "+1", "text": f"msg {i}",
        })

    backfill.run()

    # Nothing collapsed: original 5 still on disk, no backfill event, no archive.
    remaining = sorted(p.name for p in events.iterdir() if p.suffix == ".yaml")
    assert len(remaining) == 5
    for name in remaining:
        payload = yaml.safe_load((events / name).read_text())
        assert payload["kind"] != "kernel:backfill"
    assert not (var_log / "events" / "backfill").exists()


def test_per_pai_storm_isolation(backfill_env, live_dir) -> None:
    # Two PAIs; one storms (wake_on matches its kind), the other doesn't.
    # Storming PAI gets a backfill; the other's events are untouched.
    events, var_log = backfill_env
    P.spawn_pai(
        pid=3, slug="a", description="a", wake_on=["imessage:*"],
    )
    P.spawn_pai(
        pid=4, slug="b", description="b", wake_on=["email:*"],
    )

    for i in range(30):
        _write_event(events, f"20260101T00{i:04d}-imessage.yaml", {
            "source": "imessage", "kind": "ping",
        })
    for i in range(3):
        _write_event(events, f"20260101T01{i:04d}-email.yaml", {
            "source": "email", "kind": "new",
        })

    backfill.run()

    remaining = sorted(events.iterdir())
    yaml_payloads = [yaml.safe_load(p.read_text()) for p in remaining]
    kinds = [y["kind"] for y in yaml_payloads]
    # 3 email events untouched + 1 synthetic backfill for pid=3.
    assert kinds.count("kernel:backfill") == 1
    assert kinds.count("new") == 3
    synthetic = next(y for y in yaml_payloads if y["kind"] == "kernel:backfill")
    assert synthetic["target_pid"] == 3
    assert synthetic["count"] == 30
