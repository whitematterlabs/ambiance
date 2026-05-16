"""Regression tests for macmail-in cursor durability.

The bug: `_drain_live` emitted `new_email` events in a loop but only
persisted `last_announced_rowid` once, after the loop finished. A
restart in that window left the on-disk cursor stale, so `_drain_catchup`
re-announced every email whose event had already gone out — PAI got
nudged twice for the same message.

Fix (option 2): persist the cursor immediately after every `emit_event`
via `_checkpoint`, so the on-disk `last_announced_rowid` is never more
than one emit behind reality.

The driver source of record lives in `pairegistry/drivers/email/macmail/`;
these tests run against the installed copy under `usr/lib/drivers/`.
"""

from __future__ import annotations

import pytest

from drivers.email.macmail import inbound


def _row(rowid: int) -> dict:
    # ingest_row is monkeypatched in these tests, so the row only needs to
    # answer row["rowid"]. A plain dict stands in for the sqlite3.Row.
    return {"rowid": rowid, "date_received": 0, "conversation_id": 0, "url": ""}


def _result(created: bool = True) -> dict:
    return {
        "account": "owner@example.com",
        "thread_slug": "t",
        "subject": "s",
        "from": "x@y.z",
        "direction": "inbound",
        "path": "communication/email/owner@example.com/2026-05-14/s.yaml",
        "_created": created,
    }


def test_drain_live_checkpoints_cursor_after_every_emit(monkeypatch):
    """A crash mid-drain must not lose already-emitted rows: the cursor is
    saved after each emit, not just at the end of the loop."""
    saved: list[tuple[int, int]] = []
    emitted: list[int] = []

    monkeypatch.setattr(inbound, "_load_parked", lambda: {})
    monkeypatch.setattr(inbound, "_retry_parked", lambda parked, cfg: [])
    monkeypatch.setattr(inbound, "_save_parked", lambda parked: None)
    monkeypatch.setattr(
        inbound, "_save_cursor", lambda lr, la: saved.append((lr, la))
    )
    monkeypatch.setattr(
        inbound, "_query_rows", lambda lr, cfg: [_row(10), _row(11), _row(12)]
    )
    monkeypatch.setattr(inbound, "ingest_row", lambda row, cfg: _result(created=True))

    def _emit(payload):
        # Simulate a crash after the second email's event has been written.
        emitted.append(payload)
        if len(emitted) == 2:
            raise RuntimeError("simulated kill mid-drain")

    monkeypatch.setattr(inbound.P, "emit_event", _emit)

    with pytest.raises(RuntimeError, match="simulated kill"):
        inbound._drain_live(9, cfg=None, last_announced=9)

    # Pre-fix: _save_cursor only ran after the loop, so the crash lost
    # everything and `saved` would be empty. Post-fix: the first emit was
    # checkpointed before the crash.
    assert saved, "cursor was never persisted before the crash"
    assert saved[-1] == (10, 10), (
        f"expected cursor checkpointed at rowid 10 before crash, got {saved[-1]}"
    )


def test_drain_live_checkpoints_each_row_in_order(monkeypatch):
    """No crash: every emitted row produces a cursor checkpoint, advancing
    last_rowid and last_announced together."""
    saved: list[tuple[int, int]] = []

    monkeypatch.setattr(inbound, "_load_parked", lambda: {})
    monkeypatch.setattr(inbound, "_retry_parked", lambda parked, cfg: [])
    monkeypatch.setattr(inbound, "_save_parked", lambda parked: None)
    monkeypatch.setattr(
        inbound, "_save_cursor", lambda lr, la: saved.append((lr, la))
    )
    monkeypatch.setattr(
        inbound, "_query_rows", lambda lr, cfg: [_row(10), _row(11), _row(12)]
    )
    monkeypatch.setattr(inbound, "ingest_row", lambda row, cfg: _result(created=True))
    monkeypatch.setattr(inbound.P, "emit_event", lambda payload: None)

    new_last, new_announced = inbound._drain_live(9, cfg=None, last_announced=9)

    # One checkpoint per emit (10, 11, 12), each advancing both fields.
    assert (10, 10) in saved
    assert (11, 11) in saved
    assert (12, 12) in saved
    assert (new_last, new_announced) == (12, 12)


def test_drain_live_already_written_rows_are_not_emitted(monkeypatch):
    """A row whose yaml already exists (_created=False) is skipped — no
    emit, no spurious cursor churn from a re-announcement."""
    emitted: list = []

    monkeypatch.setattr(inbound, "_load_parked", lambda: {})
    monkeypatch.setattr(inbound, "_retry_parked", lambda parked, cfg: [])
    monkeypatch.setattr(inbound, "_save_parked", lambda parked: None)
    monkeypatch.setattr(inbound, "_save_cursor", lambda lr, la: None)
    monkeypatch.setattr(inbound, "_query_rows", lambda lr, cfg: [_row(10)])
    monkeypatch.setattr(
        inbound, "ingest_row", lambda row, cfg: _result(created=False)
    )
    monkeypatch.setattr(inbound.P, "emit_event", lambda p: emitted.append(p))

    new_last, new_announced = inbound._drain_live(9, cfg=None, last_announced=9)

    assert emitted == [], "already-written row must not be re-emitted"
    # Cursor still advances past the consumed rowid.
    assert new_last == 10


def test_drain_catchup_checkpoints_before_returning(monkeypatch):
    """The boot-time backlog event is followed immediately by a cursor
    save, so a crash after the emit can't trigger a re-announce next boot."""
    saved: list[tuple[int, int]] = []
    emitted: list = []

    monkeypatch.setattr(inbound, "_load_parked", lambda: {})
    monkeypatch.setattr(inbound, "_retry_parked", lambda parked, cfg: [])
    monkeypatch.setattr(inbound, "_save_parked", lambda parked: None)
    monkeypatch.setattr(
        inbound, "_save_cursor", lambda lr, la: saved.append((lr, la))
    )
    monkeypatch.setattr(
        inbound, "_query_rows", lambda lr, cfg: [_row(10), _row(11)]
    )
    monkeypatch.setattr(inbound, "ingest_row", lambda row, cfg: _result(created=True))
    monkeypatch.setattr(
        inbound, "_mac_date_to_dt", lambda secs: inbound.datetime.now()
    )
    monkeypatch.setattr(inbound.P, "emit_event", lambda p: emitted.append(p))

    new_last, new_announced = inbound._drain_catchup(9, cfg=None, last_announced=9)

    assert len(emitted) == 1 and emitted[0]["kind"] == "email_backlog"
    assert saved, "cursor not persisted after backlog emit"
    assert saved[-1] == (11, 11)
    assert (new_last, new_announced) == (11, 11)
