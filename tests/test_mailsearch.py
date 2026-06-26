"""Tests for the `mailsearch` bin tool.

mailsearch used to query only Mail.app's live SQLite Envelope Index. It now
searches the on-disk canonical yaml archive too, and merges the two sources
deduped by Message-ID — so mail Mail.app has deleted still turns up, and a
message present in both sources appears once.

The driver source of record lives in `pairegistry/`; the tool sits at
`pairegistry/bin/mailsearch/mailsearch.py`. We import it by path off the
`drivers.email` package location so the test follows the registry wherever
it lives.
"""

from __future__ import annotations

import importlib.util
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

import drivers.email as _de

_REG_ROOT = Path(_de.__file__).resolve().parents[2]
_MS_PATH = _REG_ROOT / "bin" / "mailsearch" / "mailsearch.py"

_spec = importlib.util.spec_from_file_location("mailsearch_under_test", _MS_PATH)
ms = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ms)


def _write_msg(root: Path, account: str, datedir: str, name: str, msg: dict) -> Path:
    d = root / "var" / "spool" / "communication" / "email" / account / datedir
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text(yaml.safe_dump(msg, sort_keys=False, allow_unicode=True))
    return p


@pytest.fixture
def spool(tmp_path, monkeypatch):
    monkeypatch.setattr(ms.paths, "PAI_ROOT", tmp_path, raising=True)
    return tmp_path


def test_search_disk_matches_from_and_subject(spool):
    _write_msg(spool, "owner@example.com", "2025-01-02", "q3-budget.yaml", {
        "message_id": "<a@x>", "from": "bob@acme.com", "from_name": "Bob Smith",
        "to": ["owner@example.com"], "cc": [], "bcc": [],
        "subject": "Q3 budget review", "direction": "inbound",
        "received_at": "2025-01-02T09:00:00+00:00",
    })
    _write_msg(spool, "owner@example.com", "2025-01-03", "lunch.yaml", {
        "message_id": "<b@x>", "from": "carol@acme.com", "from_name": "Carol",
        "to": ["owner@example.com"], "cc": [], "bcc": [],
        "subject": "lunch?", "direction": "inbound",
        "received_at": "2025-01-03T09:00:00+00:00",
    })

    # --from matches address substring
    hits = ms.search_disk(ms.parse_args(["--from", "bob@acme.com"]))
    assert [h["subject"] for h in hits] == ["Q3 budget review"]
    assert hits[0]["path"].startswith("communication/email/")
    assert hits[0]["account"] == "owner@example.com"

    # --from matches display name too
    assert ms.search_disk(ms.parse_args(["--from", "Bob Smith"]))[0]["_mid"] == "<a@x>"

    # --subject substring, case-insensitive
    assert len(ms.search_disk(ms.parse_args(["--subject", "BUDGET"]))) == 1


def test_search_disk_skips_threads_meta_and_drafts(spool):
    base = spool / "var" / "spool" / "communication" / "email"
    _write_msg(spool, "owner@example.com", "2025-01-02", "real.yaml", {
        "message_id": "<a@x>", "from": "bob@acme.com",
        "subject": "real message", "direction": "inbound",
        "received_at": "2025-01-02T09:00:00+00:00",
    })
    # account metadata — not a message
    (base / "owner@example.com" / "meta.yaml").write_text("account: owner@example.com\n")
    # threads/ index — symlink-equivalent noise that would double-count
    tdir = base / "owner@example.com" / "threads" / "t-1234"
    tdir.mkdir(parents=True)
    (tdir / "2025-01-02T09-00-real.yaml").write_text(yaml.safe_dump({
        "message_id": "<a@x>", "from": "bob@acme.com", "subject": "real message",
        "direction": "inbound", "received_at": "2025-01-02T09:00:00+00:00",
    }))
    # drafts account — unsent, never returned
    _write_msg(spool, "drafts", "2025-01-02", "draft.yaml", {
        "message_id": "<d@x>", "from": "owner@example.com",
        "subject": "draft message", "direction": "outbound",
        "sent_at": "2025-01-02T09:00:00+00:00",
    })

    hits = ms.search_disk(ms.parse_args(["--subject", "message"]))
    assert [h["subject"] for h in hits] == ["real message"]


def test_search_disk_since_and_inbox_only(spool):
    _write_msg(spool, "owner@example.com", "2024-12-31", "old.yaml", {
        "message_id": "<old@x>", "from": "bob@acme.com", "subject": "old",
        "direction": "inbound", "received_at": "2024-12-31T09:00:00+00:00",
    })
    _write_msg(spool, "owner@example.com", "2025-02-01", "new.yaml", {
        "message_id": "<new@x>", "from": "bob@acme.com", "subject": "new",
        "direction": "inbound", "received_at": "2025-02-01T09:00:00+00:00",
    })
    _write_msg(spool, "owner@example.com", "2025-02-02", "sent.yaml", {
        "message_id": "<sent@x>", "from": "owner@example.com", "subject": "sent",
        "direction": "outbound", "sent_at": "2025-02-02T09:00:00+00:00",
    })

    since = ms.search_disk(ms.parse_args(["--from", "bob", "--since", "2025-01-15"]))
    assert [h["_mid"] for h in since] == ["<new@x>"]

    inbox = ms.search_disk(ms.parse_args(["--from", "owner@example.com", "--inbox-only"]))
    assert inbox == []  # the only owner-from message is outbound


def _hit(path, mid, dt):
    return {"path": path, "date": dt.isoformat(), "account": "a",
            "from": "f", "subject": "s", "_mid": mid, "_sort": dt}


def test_merge_dedupe_by_message_id_prefers_sqlite():
    dt = datetime(2025, 1, 1, tzinfo=timezone.utc)
    sqlite_hit = _hit("communication/email/a/2025-01-01/live.yaml", "<dup@x>", dt)
    disk_hit = _hit("communication/email/a/2025/01/01/archived.yaml", "<dup@x>", dt)

    out = ms.merge_dedupe([sqlite_hit], [disk_hit], limit=10)
    assert len(out) == 1
    assert out[0]["path"].endswith("live.yaml")  # sqlite copy wins


def test_merge_dedupe_keeps_disk_only_hits_and_sorts_newest_first():
    d1 = datetime(2025, 1, 1, tzinfo=timezone.utc)
    d2 = datetime(2025, 3, 1, tzinfo=timezone.utc)
    d3 = datetime(2025, 2, 1, tzinfo=timezone.utc)
    s = _hit("p_sql", "<s@x>", d1)
    disk = [_hit("p_d2", "<d2@x>", d2), _hit("p_d3", "<d3@x>", d3)]

    out = ms.merge_dedupe([s], disk, limit=10)
    assert [h["_mid"] for h in out] == ["<d2@x>", "<d3@x>", "<s@x>"]


def test_merge_dedupe_caps_at_limit():
    hits = [
        _hit(f"p{i}", f"<{i}@x>", datetime(2025, 1, i + 1, tzinfo=timezone.utc))
        for i in range(5)
    ]
    out = ms.merge_dedupe(hits, [], limit=2)
    assert len(out) == 2
    # newest two (Jan 5, Jan 4)
    assert [h["_mid"] for h in out] == ["<4@x>", "<3@x>"]


def test_dedupe_falls_back_to_path_when_no_message_id():
    dt = datetime(2025, 1, 1, tzinfo=timezone.utc)
    a = _hit("same/path.yaml", "", dt)
    b = _hit("same/path.yaml", "", dt)
    assert len(ms.merge_dedupe([a], [b], limit=10)) == 1
