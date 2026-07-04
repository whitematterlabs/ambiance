"""Regression tests for imessage-backfill driver guards."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import importlib
import sqlite3
import sys
import types

import pytest

from boot import processes as P


MAC_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)


def _write_empty_chat_db(path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE message (
                text TEXT,
                attributedBody BLOB,
                is_from_me INTEGER,
                date INTEGER,
                handle TEXT,
                chat_guid TEXT,
                participant_count INTEGER
            );
            """
        )
    finally:
        conn.close()


@pytest.fixture
def imessage_backfill_module(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """Import bin.imessage_backfill with fake driver modules.

    The source imports installed driver packages at module import time. These
    tests only exercise the guard, so local stubs keep the test hermetic.
    """
    old_module = sys.modules.pop("bin.imessage_backfill", None)

    drivers_pkg = types.ModuleType("drivers")
    drivers_pkg.__path__ = []
    imessage_pkg = types.ModuleType("drivers.imessage")
    imessage_pkg.__path__ = []
    inbound = types.ModuleType("drivers.imessage.inbound")
    messages = types.ModuleType("drivers.messages")

    chat_db = tmp_path / "chat.db"
    messages_dir = tmp_path / "messages"
    saved_cursor: list[int] = []

    inbound.CHAT_DB = chat_db
    inbound.CURSOR_PATH = tmp_path / "cursor.yaml"
    inbound.DELTA_SQL = """
        SELECT
            m.ROWID AS rowid,
            m.text AS text,
            m.attributedBody AS attributed_body,
            m.is_from_me AS is_from_me,
            m.date AS mac_date,
            m.handle AS handle,
            m.chat_guid AS chat_guid,
            m.participant_count AS participant_count
        FROM message m
        WHERE m.ROWID > ?
        ORDER BY m.ROWID ASC
    """
    inbound.MAC_EPOCH = MAC_EPOCH
    inbound._decode_attributed_body = lambda data: None
    inbound._load_cursor = lambda: 0
    inbound._mac_date_to_iso = lambda mac_date: (
        MAC_EPOCH + timedelta(seconds=mac_date / 1e9)
    ).astimezone().isoformat(timespec="seconds")
    inbound._save_cursor = lambda rowid: saved_cursor.append(rowid)

    messages.MESSAGES_DIR = messages_dir

    def _ingest(**_kwargs):
        day_file = messages_dir / "thread" / "2026-01-01.md"
        day_file.parent.mkdir(parents=True, exist_ok=True)
        day_file.touch()
        return types.SimpleNamespace(day_file=day_file)

    messages.ingest = _ingest

    drivers_pkg.imessage = imessage_pkg
    imessage_pkg.inbound = inbound
    drivers_pkg.messages = messages
    monkeypatch.setitem(sys.modules, "drivers", drivers_pkg)
    monkeypatch.setitem(sys.modules, "drivers.imessage", imessage_pkg)
    monkeypatch.setitem(sys.modules, "drivers.imessage.inbound", inbound)
    monkeypatch.setitem(sys.modules, "drivers.messages", messages)

    module = importlib.import_module("bin.imessage_backfill")
    yield module, saved_cursor

    sys.modules.pop("bin.imessage_backfill", None)
    if old_module is not None:
        sys.modules["bin.imessage_backfill"] = old_module


@pytest.fixture
def isolated_proc(tmp_path, monkeypatch: pytest.MonkeyPatch):
    proc = tmp_path / "proc"
    events = tmp_path / "events"
    home = tmp_path / "home"
    proc.mkdir()
    events.mkdir()
    home.mkdir()
    monkeypatch.setattr(P, "PROC_DIR", proc, raising=True)
    monkeypatch.setattr(P, "EVENTS_DIR", events, raising=True)
    monkeypatch.setattr(P, "HOME_DIR", home, raising=True)
    return proc


def test_running_inbound_driver_does_not_block_backfill(
    imessage_backfill_module, isolated_proc, capsys
) -> None:
    module, _saved_cursor = imessage_backfill_module
    _write_empty_chat_db(module.CHAT_DB)
    module.P.spawn(module.IN_DRIVER_SLUG, {"kind": "driver", "active": True})

    rc = module.backfill(date(2026, 1, 1), date(2026, 1, 1), seed=True)

    assert rc == 0
    assert "refusing" not in capsys.readouterr().err


def test_running_outbound_driver_still_blocks_backfill(
    imessage_backfill_module, isolated_proc, capsys
) -> None:
    module, _saved_cursor = imessage_backfill_module
    _write_empty_chat_db(module.CHAT_DB)
    module.P.spawn(module.OUT_DRIVER_SLUG, {"kind": "driver", "active": True})

    rc = module.backfill(date(2026, 1, 1), date(2026, 1, 1), seed=True)

    err = capsys.readouterr().err
    assert rc == 3
    assert "imessage-out running" in err
    assert "imessage-in" not in err


def test_seed_outbound_cursors_uses_fhs_relative_keys(
    imessage_backfill_module, tmp_path, monkeypatch
) -> None:
    module, _saved_cursor = imessage_backfill_module
    pai_root = tmp_path / "pai_root"
    day_file = (
        pai_root
        / "var"
        / "spool"
        / "communication"
        / "messages"
        / "thread"
        / "2026-01-01.md"
    )
    day_file.parent.mkdir(parents=True)
    day_file.write_text("[12:00] me: historical\n")

    # monkeypatch (not direct assignment) so PAI_ROOT is restored — a bare
    # `module.paths.PAI_ROOT = ...` leaks the tmp root into later tests that
    # read paths.PAI_ROOT at module scope (e.g. test_skill_candidate).
    monkeypatch.setattr(module.paths, "PAI_ROOT", pai_root, raising=True)
    monkeypatch.setattr(module, "OUT_CURSORS", tmp_path / "cursors.yaml", raising=True)

    assert module._seed_outbound_cursors({day_file}) == 1
    with module.OUT_CURSORS.open() as f:
        cursors = module.yaml.safe_load(f) or {}

    assert cursors == {
        "var/spool/communication/messages/thread/2026-01-01.md": day_file.stat().st_size
    }
