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

from email.message import EmailMessage
import sqlite3

import pytest

from drivers.email.macmail import accounts as A
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


def test_delta_query_includes_read_and_unread_messages():
    """The live driver must ingest all new inbox/sent rows, regardless of
    Mail.app's read flag. Read/unread is a user-facing state, not a driver
    routing predicate."""
    cfg = A.AccountsConfig(
        accounts={
            "ACCOUNT-UUID": A.Account(
                uuid="ACCOUNT-UUID",
                addresses=["owner@example.com"],
                inbox_name="INBOX",
            )
        }
    )
    sql, patterns = inbound._build_delta_sql(cfg)

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE mailboxes (
            ROWID INTEGER PRIMARY KEY,
            url TEXT NOT NULL
        );
        CREATE TABLE messages (
            ROWID INTEGER PRIMARY KEY,
            mailbox INTEGER NOT NULL,
            date_received INTEGER NOT NULL,
            conversation_id INTEGER NOT NULL,
            read INTEGER NOT NULL
        );
        INSERT INTO mailboxes (ROWID, url)
        VALUES (1, 'imap://ACCOUNT-UUID/INBOX');
        INSERT INTO messages (ROWID, mailbox, date_received, conversation_id, read)
        VALUES
            (10, 1, 1800000000, 1, 0),
            (11, 1, 1800000001, 1, 1);
        """
    )

    rows = conn.execute(sql, (9, *patterns)).fetchall()

    assert [int(row["rowid"]) for row in rows] == [10, 11]


def test_delta_query_includes_gmail_all_mail_rows():
    """Gmail messages visible in Mail.app's Inbox may be indexed under
    [Gmail]/All Mail instead of INBOX."""
    cfg = A.AccountsConfig(
        accounts={
            "ACCOUNT-UUID": A.Account(
                uuid="ACCOUNT-UUID",
                addresses=["owner@example.com"],
                inbox_name="INBOX",
                all_mail_name="All Mail",
            )
        }
    )
    sql, patterns = inbound._build_delta_sql(cfg)

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE mailboxes (
            ROWID INTEGER PRIMARY KEY,
            url TEXT NOT NULL
        );
        CREATE TABLE messages (
            ROWID INTEGER PRIMARY KEY,
            mailbox INTEGER NOT NULL,
            date_received INTEGER NOT NULL,
            conversation_id INTEGER NOT NULL
        );
        INSERT INTO mailboxes (ROWID, url)
        VALUES (1, 'imap://ACCOUNT-UUID/%5BGmail%5D/All%20Mail');
        INSERT INTO messages (ROWID, mailbox, date_received, conversation_id)
        VALUES (12, 1, 1800000002, 1);
        """
    )

    rows = conn.execute(sql, (9, *patterns)).fetchall()

    assert [int(row["rowid"]) for row in rows] == [12]


def test_all_mail_direction_uses_from_address():
    cfg = A.AccountsConfig(
        accounts={
            "ACCOUNT-UUID": A.Account(
                uuid="ACCOUNT-UUID",
                addresses=["owner@example.com"],
                all_mail_name="All Mail",
            )
        }
    )
    incoming = EmailMessage()
    incoming["From"] = "Mercor <team@mercor.com>"
    outgoing = EmailMessage()
    outgoing["From"] = "Owner <owner@example.com>"

    assert (
        inbound._direction_for_message(
            "inbound", incoming, cfg, "ACCOUNT-UUID", all_mail=True
        )
        == "inbound"
    )
    assert (
        inbound._direction_for_message(
            "inbound", outgoing, cfg, "ACCOUNT-UUID", all_mail=True
        )
        == "outbound"
    )


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


def test_index_access_reports_denied_not_absent(monkeypatch):
    """A TCC denial (EPERM) must be classified 'denied', not masked as
    'absent'. `Path.exists()` returns False under a Full Disk Access denial,
    which used to make a permission wall look like a missing mailbox and idle
    the driver with the wrong message."""
    # Permission denied → 'denied'
    def _eperm(path, flags):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(inbound.os, "open", _eperm)
    assert inbound._index_access() == "denied"

    # Genuinely missing → 'absent'
    def _enoent(path, flags):
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(inbound.os, "open", _enoent)
    assert inbound._index_access() == "absent"

    # Readable → 'ok', and the probe fd is closed.
    closed: list[int] = []
    monkeypatch.setattr(inbound.os, "open", lambda path, flags: 4242)
    monkeypatch.setattr(inbound.os, "close", lambda fd: closed.append(fd))
    assert inbound._index_access() == "ok"
    assert closed == [4242]


def test_run_surfaces_fda_hint_on_denied_access(monkeypatch, capsys):
    """When the Mail store is unreadable, run() prints an actionable Full Disk
    Access hint and exits cleanly — without the old "not found" mislabel and
    without proceeding into account discovery (which would hit the same wall)."""
    monkeypatch.setattr(inbound, "_index_access", lambda: "denied")

    def _must_not_run(*a, **k):
        raise AssertionError("run() must bail before account discovery")

    monkeypatch.setattr(inbound.A, "refresh", _must_not_run)

    import asyncio

    asyncio.run(inbound.run())

    out = capsys.readouterr().out
    assert "Full Disk Access" in out
    assert "not found" not in out


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
