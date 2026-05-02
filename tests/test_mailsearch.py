"""mailsearch tests — SQL builder, argparse guards, output shape.

No live DB: we don't open the real Envelope Index in tests. The end-to-end
smoke test is manual (run mailsearch against your real Mail).
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone

import pytest

from bin import mailsearch as M
from drivers.email.macmail import accounts as A


_FAKE_CFG = A.parse_discovery_output(
    "ADDR|UUID-IMAP|me@icloud.com\n"
    "INBOX|UUID-IMAP|INBOX\n"
    "SENT|UUID-IMAP|Sent Messages\n"
    "ADDR|UUID-EWS|me@outlook.com\n"
    "INBOX|UUID-EWS|Gelen Kutusu\n"
    "SENT|UUID-EWS|Gönderilmiş Öğeler\n"
)


def _ns(**kwargs):
    """Build a Namespace with all fields the SQL builder reads, defaulted."""
    base = dict(
        from_addr=None,
        to_addr=None,
        subject=None,
        account=None,
        since=None,
        until=None,
        since_ts=None,
        until_ts=None,
        unread=False,
        flagged=False,
        inbox_only=False,
        limit=20,
    )
    base.update(kwargs)
    import argparse
    return argparse.Namespace(**base)


# ---------- argparse guards ------------------------------------------------

def test_requires_at_least_one_filter() -> None:
    with pytest.raises(SystemExit):
        M.parse_args([])


def test_accepts_from_alone() -> None:
    args = M.parse_args(["--from", "bob@example.com"])
    assert args.from_addr == "bob@example.com"
    assert args.limit == M.DEFAULT_LIMIT


def test_accepts_subject_alone() -> None:
    args = M.parse_args(["--subject", "lunch"])
    assert args.subject == "lunch"


def test_rejects_zero_limit() -> None:
    with pytest.raises(SystemExit):
        M.parse_args(["--from", "x", "--limit", "0"])


def test_rejects_excessive_limit() -> None:
    with pytest.raises(SystemExit):
        M.parse_args(["--from", "x", "--limit", str(M.MAX_LIMIT + 1)])


def test_parses_dates_to_unix_seconds() -> None:
    args = M.parse_args(["--since", "2025-01-01"])
    expected = int(
        datetime.combine(date(2025, 1, 1), time.min)
        .astimezone(timezone.utc).timestamp()
    )
    assert args.since_ts == expected


def test_rejects_bad_date() -> None:
    with pytest.raises(SystemExit):
        M.parse_args(["--since", "yesterday"])


# ---------- SQL builder ----------------------------------------------------

def test_default_includes_inbox_and_sent() -> None:
    sql, params = M.build_query(_ns(from_addr="bob"), _FAKE_CFG)
    inbox_present = any("INBOX" in str(p) for p in params[:4])
    sent_present = any("Sent" in str(p) for p in params[:4])
    assert inbox_present and sent_present


def test_includes_localized_mailbox_names() -> None:
    """Turkish (EWS Outlook) inbox/sent names appear as URL-encoded NFD."""
    _, params = M.build_query(_ns(from_addr="bob"), _FAKE_CFG)
    pat_blob = "\n".join(str(p) for p in params)
    assert "Gelen%20Kutusu" in pat_blob
    # Combining diaeresis on Ö in `Gönderilmiş` etc.
    assert "%CC%88" in pat_blob


def test_inbox_only_drops_sent_patterns() -> None:
    _, params = M.build_query(_ns(from_addr="bob", inbox_only=True), _FAKE_CFG)
    sent_present = any("Sent" in str(p) or "G%C3" in str(p) or "%CC%A7" in str(p) for p in params[:4])
    # Outbound EWS pattern (`Gönderilmiş Öğeler`) carries a combining
    # cedilla `%CC%A7` not present in any inbox name. If absent, sent
    # patterns were correctly dropped.
    assert not any("%CC%A7" in str(p) for p in params)
    # And no `Sent Messages` either.
    assert not any("Sent" in str(p) for p in params)


def test_from_filter_emits_marker_and_pattern() -> None:
    _, params = M.build_query(_ns(from_addr="bob@example.com"), _FAKE_CFG)
    # marker + LIKE pattern should both appear after the mailbox params.
    assert 1 in params
    assert "%bob@example.com%" in params


def test_unset_filters_pass_none_marker() -> None:
    _, params = M.build_query(_ns(from_addr="bob"), _FAKE_CFG)
    # subject/to/account weren't supplied — three (None, None) pairs in the
    # filter section.
    none_pairs = sum(1 for p in params if p is None)
    assert none_pairs >= 6  # subject, to, account, since*2, until*2


def test_unread_flag_sets_marker() -> None:
    _, params = M.build_query(_ns(from_addr="bob", unread=True), _FAKE_CFG)
    # unread marker appears as `1` (not None) toward the end.
    assert params[-3] == 1  # unread
    assert params[-2] is None  # flagged unset


def test_flagged_flag_sets_marker() -> None:
    _, params = M.build_query(_ns(from_addr="bob", flagged=True), _FAKE_CFG)
    assert params[-3] is None
    assert params[-2] == 1


def test_limit_is_last_param() -> None:
    _, params = M.build_query(_ns(from_addr="bob", limit=42), _FAKE_CFG)
    assert params[-1] == 42


def test_date_bounds_emit_marker_and_value() -> None:
    args = _ns(from_addr="bob", since_ts=1700000000, until_ts=1800000000)
    _, params = M.build_query(args, _FAKE_CFG)
    # since/until each get (marker, value) — markers are the timestamps.
    assert 1700000000 in params
    assert 1800000000 in params


def test_empty_cfg_produces_match_nothing_clause() -> None:
    sql, params = M.build_query(_ns(from_addr="bob"), A.AccountsConfig())
    assert "1 = 0" in sql


def test_sql_is_a_single_select() -> None:
    sql, _ = M.build_query(_ns(from_addr="bob"), _FAKE_CFG)
    # Only one top-level SELECT (the EXISTS subquery has its own; ignore that).
    top_level = sql.strip().split("(")[0]
    assert top_level.strip().upper().startswith("SELECT")
    assert "ORDER BY m.date_received DESC" in sql
    assert "LIMIT ?" in sql


# ---------- run_search wiring ----------------------------------------------

class _FakeRow(dict):
    """sqlite3.Row stand-in that supports both index and key access."""

    def __getitem__(self, k):  # type: ignore[override]
        return dict.__getitem__(self, k)


def _stub_refresh(monkeypatch, cfg: A.AccountsConfig) -> None:
    async def fake() -> A.AccountsConfig:
        return cfg
    monkeypatch.setattr(M.A, "refresh", fake)


def test_run_search_emits_yaml_for_hits(monkeypatch, tmp_path, capsys) -> None:
    import bin.mailsearch as ms

    # Fake Envelope Index: pretend it exists and the connect returns a fake conn.
    monkeypatch.setattr(ms.IN, "ENVELOPE_INDEX", tmp_path / "Envelope Index")
    (tmp_path / "Envelope Index").touch()
    _stub_refresh(monkeypatch, _FAKE_CFG)

    fake_rows = [
        _FakeRow(
            rowid=1,
            date_received=int(datetime(2025, 8, 12, 14, 32, tzinfo=timezone.utc).timestamp()),
            conversation_id=99,
            url="imap://uuid-x/INBOX",
        ),
        _FakeRow(
            rowid=2,
            date_received=int(datetime(2025, 8, 11, 9, 0, tzinfo=timezone.utc).timestamp()),
            conversation_id=100,
            url="imap://uuid-x/INBOX",
        ),
    ]

    class FakeConn:
        def execute(self, sql, params):
            class C:
                def fetchall(self_inner):
                    return fake_rows
            return C()

        def close(self):
            pass

    monkeypatch.setattr(ms.IN, "_connect", lambda: FakeConn())

    ingest_calls: list[int] = []

    def fake_ingest(row, cfg):
        ingest_calls.append(int(row["rowid"]))
        return {
            "account": "arda@example.com",
            "thread_slug": "x-abc",
            "subject": f"hit {row['rowid']}",
            "from": "bob@example.com",
            "direction": "inbound",
            "path": f"var/spool/communication/email/arda@example.com/2025-08-12/hit-{row['rowid']}.yaml",
            "_created": True,
        }

    monkeypatch.setattr(ms.IN, "ingest_row", fake_ingest)

    args = ms.parse_args(["--from", "bob"])
    rc = ms.run_search(args)

    assert rc == 0
    assert ingest_calls == [1, 2]
    out = capsys.readouterr().out
    assert "from: bob@example.com" in out
    assert "subject: hit 1" in out
    assert "var/spool/communication/email/arda@example.com" in out


def test_run_search_skips_partial_emlx(monkeypatch, tmp_path, capsys) -> None:
    import bin.mailsearch as ms

    monkeypatch.setattr(ms.IN, "ENVELOPE_INDEX", tmp_path / "Envelope Index")
    (tmp_path / "Envelope Index").touch()
    _stub_refresh(monkeypatch, _FAKE_CFG)

    fake_row = _FakeRow(rowid=7, date_received=0, conversation_id=0, url="imap://x/INBOX")

    class FakeConn:
        def execute(self, sql, params):
            class C:
                def fetchall(self_inner):
                    return [fake_row]
            return C()
        def close(self):
            pass

    monkeypatch.setattr(ms.IN, "_connect", lambda: FakeConn())
    monkeypatch.setattr(ms.IN, "ingest_row", lambda row, cfg: None)

    args = ms.parse_args(["--from", "bob"])
    rc = ms.run_search(args)
    assert rc == 0

    captured = capsys.readouterr()
    assert "[]" in captured.out
    assert "skipped rowid=7" in captured.err


def test_run_search_aborts_when_no_accounts_discovered(monkeypatch, tmp_path, capsys) -> None:
    """If AppleScript discovery turns up nothing (Mail.app down, no
    automation permission), bail out instead of running a `1=0` query."""
    import bin.mailsearch as ms

    monkeypatch.setattr(ms.IN, "ENVELOPE_INDEX", tmp_path / "Envelope Index")
    (tmp_path / "Envelope Index").touch()
    _stub_refresh(monkeypatch, A.AccountsConfig())

    args = ms.parse_args(["--from", "bob"])
    rc = ms.run_search(args)
    assert rc == 2
    assert "no Mail.app accounts" in capsys.readouterr().err
