"""Outbound macmail driver tests.

Covers the AppleScript builders, draft state machine, and the retry path.
osascript itself is mocked — these tests never shell out to Mail.app.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import yaml

from boot import paths
from boot import processes as P
from drivers.email.macmail import accounts as A
from drivers.email.macmail import outbound


# ---------- fixtures -------------------------------------------------------

@pytest.fixture
def email_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect EMAIL_ROOT/DRAFTS_DIR + processes paths into tmp_path."""
    root = tmp_path / "email"
    drafts = root / "drafts"
    drafts.mkdir(parents=True)
    monkeypatch.setattr(outbound, "EMAIL_ROOT", root, raising=True)
    monkeypatch.setattr(outbound, "DRAFTS_DIR", drafts, raising=True)
    monkeypatch.setattr(paths, "PAI_ROOT", tmp_path, raising=True)
    events_dir = tmp_path / "events"
    events_dir.mkdir()
    monkeypatch.setattr(P, "EVENTS_DIR", events_dir, raising=True)
    return root


@pytest.fixture
def known_accounts(monkeypatch: pytest.MonkeyPatch) -> set[str]:
    """Pin the AppleScript-derived account cache to a fixed config.

    Two accounts: a primary with a relay alias (mirrors the iCloud
    Hide-My-Email shape), and a secondary EWS-style account with a
    localized inbox/sent name.
    """
    cfg = A.parse_discovery_output(
        "ADDR|U1|user@example.com\n"
        "ADDR|U1|alias@privaterelay.example.invalid\n"
        "INBOX|U1|INBOX\n"
        "SENT|U1|Sent Messages\n"
        "ADDR|U2|user@example.org\n"
        "INBOX|U2|Gelen Kutusu\n"
        "SENT|U2|Gönderilmiş Öğeler\n"
    )
    monkeypatch.setattr(outbound, "_accounts_cfg", cfg, raising=True)
    return {"user@example.com", "user@example.org", "alias@privaterelay.example.invalid"}


def _write_draft(drafts_dir: Path, name: str, body: dict) -> Path:
    path = drafts_dir / f"{name}.yaml"
    path.write_text(yaml.safe_dump(body, sort_keys=False))
    return path


def _read_draft(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def _process(path: Path) -> None:
    asyncio.run(outbound._process(path))


def _stub_osa(monkeypatch: pytest.MonkeyPatch, returncode: int, stderr: str = "") -> list[str]:
    calls: list[str] = []

    async def fake(script: str) -> tuple[int, str]:
        calls.append(script)
        return (returncode, stderr)

    monkeypatch.setattr(outbound, "_run_osascript", fake)
    return calls


# ---------- script builders ------------------------------------------------

def test_new_message_script_pins_sender() -> None:
    script = outbound._build_new_message_script(
        "user@example.com",
        {"subject": "hi", "content": "hello", "to": ["bob@example.com"]},
    )
    assert 'sender:"user@example.com"' in script
    assert 'subject:"hi"' in script
    assert 'address:"bob@example.com"' in script
    assert "save" in script


def test_new_message_script_handles_cc_bcc() -> None:
    script = outbound._build_new_message_script(
        "user@example.com",
        {
            "subject": "hi",
            "content": "x",
            "to": ["a@x.com"],
            "cc": ["b@x.com"],
            "bcc": ["c@x.com"],
        },
    )
    assert "to recipient" in script
    assert "cc recipient" in script
    assert "bcc recipient" in script


def test_reply_script_pins_sender_and_closes_window() -> None:
    script = outbound._build_reply_script(
        "user@example.com",
        {"in_reply_to": "<abc@xyz>", "content": "thx"},
    )
    # macOS 15 dropped `opens window` on `reply` — must not appear or
    # osascript fails with -2741.
    assert "opens window" not in script
    assert 'set sender of replyMsg to "user@example.com"' in script
    assert 'message id is "<abc@xyz>"' in script
    # Reply window briefly opens; we close it after save.
    assert "close (every window" in script


def test_esc_handles_quotes_and_backslashes() -> None:
    assert outbound._esc('a"b') == 'a\\"b'
    assert outbound._esc("a\\b") == "a\\\\b"


# ---------- _is_draft_path -------------------------------------------------

def test_is_draft_path_accepts_top_level_drafts(email_root: Path) -> None:
    p = email_root / "drafts" / "x.yaml"
    p.touch()
    assert outbound._is_draft_path(p)


def test_is_draft_path_rejects_per_account_drafts(email_root: Path) -> None:
    old = email_root / "user@example.com" / "drafts" / "x.yaml"
    old.parent.mkdir(parents=True)
    old.touch()
    assert not outbound._is_draft_path(old)


def test_is_draft_path_rejects_received_yaml(email_root: Path) -> None:
    received = email_root / "user@example.com" / "2026-05-01" / "msg.yaml"
    received.parent.mkdir(parents=True)
    received.touch()
    assert not outbound._is_draft_path(received)


def test_is_draft_path_rejects_tmp_files(email_root: Path) -> None:
    p = email_root / "drafts" / "x.yaml.tmp"
    p.touch()
    assert not outbound._is_draft_path(p)


# ---------- _process state machine -----------------------------------------

def test_process_drafts_a_new_message(
    email_root: Path, known_accounts: set[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _stub_osa(monkeypatch, 0)
    path = _write_draft(
        email_root / "drafts",
        "hello",
        {"from": "user@example.com", "to": ["bob@x.com"], "subject": "hi", "content": "yo"},
    )
    _process(path)

    draft = _read_draft(path)
    assert draft["draft_state"] == "drafted"
    assert "drafted_at" in draft
    assert len(calls) == 1
    assert 'sender:"user@example.com"' in calls[0]


def test_process_skips_terminal_drafted(
    email_root: Path, known_accounts: set[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _stub_osa(monkeypatch, 0)
    path = _write_draft(
        email_root / "drafts",
        "done",
        {
            "from": "user@example.com",
            "to": ["x@x.com"],
            "subject": "x",
            "content": "x",
            "draft_state": "drafted",
        },
    )
    _process(path)
    assert calls == []


def test_process_skips_terminal_failed_no_retry_loop(
    email_root: Path, known_accounts: set[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: previously `mail_app_drafted: false` was falsy, causing
    the driver to re-process failed drafts forever."""
    calls = _stub_osa(monkeypatch, 0)
    path = _write_draft(
        email_root / "drafts",
        "broken",
        {
            "from": "user@example.com",
            "to": ["x@x.com"],
            "subject": "x",
            "content": "x",
            "draft_state": "failed",
            "draft_error": "previously broken",
        },
    )
    _process(path)
    _process(path)  # second pass — definitely no-op
    assert calls == []


def test_process_marks_failed_on_osascript_error(
    email_root: Path, known_accounts: set[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_osa(monkeypatch, 1, "Mail got mad")
    path = _write_draft(
        email_root / "drafts",
        "boom",
        {"from": "user@example.com", "to": ["x@x.com"], "subject": "x", "content": "x"},
    )
    _process(path)
    draft = _read_draft(path)
    assert draft["draft_state"] == "failed"
    assert "Mail got mad" in draft["draft_error"]


def test_process_rejects_unknown_from(
    email_root: Path, known_accounts: set[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _stub_osa(monkeypatch, 0)
    path = _write_draft(
        email_root / "drafts",
        "wrong",
        {"from": "stranger@nowhere.invalid", "to": ["x@x.com"], "subject": "x", "content": "x"},
    )
    _process(path)
    draft = _read_draft(path)
    assert draft["draft_state"] == "failed"
    assert "no Mail.app account" in draft["draft_error"]
    assert calls == []


def test_process_rejects_missing_from(
    email_root: Path, known_accounts: set[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(script: str) -> tuple[int, str]:
        raise AssertionError("should not be called")

    monkeypatch.setattr(outbound, "_run_osascript", boom)
    path = _write_draft(
        email_root / "drafts",
        "noaddr",
        {"to": ["x@x.com"], "subject": "x", "content": "x"},
    )
    _process(path)
    draft = _read_draft(path)
    assert draft["draft_state"] == "failed"
    assert "from:" in draft["draft_error"]


def test_process_skips_when_validation_disabled(
    email_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty config means AppleScript discovery failed — fall through
    and let Mail decide rather than rejecting every draft."""
    monkeypatch.setattr(outbound, "_accounts_cfg", A.AccountsConfig(), raising=True)
    _stub_osa(monkeypatch, 0)
    path = _write_draft(
        email_root / "drafts",
        "anything",
        {"from": "anyone@anywhere.com", "to": ["x@x.com"], "subject": "x", "content": "x"},
    )
    _process(path)
    assert _read_draft(path)["draft_state"] == "drafted"


def test_process_accepts_alias_address(
    email_root: Path, known_accounts: set[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Aliases returned by Mail.app's `email addresses of account` (e.g.
    iCloud Hide-My-Email relay addresses) should be valid `from:` values,
    not rejected."""
    _stub_osa(monkeypatch, 0)
    path = _write_draft(
        email_root / "drafts",
        "alias",
        {
            "from": "alias@privaterelay.example.invalid",
            "to": ["x@x.com"],
            "subject": "x",
            "content": "x",
        },
    )
    _process(path)
    assert _read_draft(path)["draft_state"] == "drafted"


# ---------- reply parent retry --------------------------------------------

def test_process_retries_when_parent_not_found(
    email_root: Path, known_accounts: set[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_osa(monkeypatch, 1, "execution error: parent message not found (-2700)")
    scheduled: list[tuple[Path, float]] = []
    monkeypatch.setattr(
        outbound, "_schedule_retry",
        lambda path, delay: scheduled.append((path, delay)),
    )

    path = _write_draft(
        email_root / "drafts",
        "reply",
        {
            "from": "user@example.com",
            "in_reply_to": "<abc@xyz>",
            "content": "thx",
        },
    )
    _process(path)
    draft = _read_draft(path)
    assert draft["draft_state"] == "pending_parent"
    assert draft["draft_retries"] == 1
    assert scheduled == [(path, outbound.REPLY_RETRY_DELAYS[0])]


def test_process_gives_up_after_all_retries(
    email_root: Path, known_accounts: set[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_osa(monkeypatch, 1, "parent message not found")
    monkeypatch.setattr(outbound, "_schedule_retry", lambda *a, **kw: None)
    path = _write_draft(
        email_root / "drafts",
        "reply",
        {
            "from": "user@example.com",
            "in_reply_to": "<abc@xyz>",
            "content": "thx",
            "draft_retries": len(outbound.REPLY_RETRY_DELAYS),
        },
    )
    _process(path)
    draft = _read_draft(path)
    assert draft["draft_state"] == "failed"
    assert "parent message not found" in draft["draft_error"]


# ---------- _scan_existing -------------------------------------------------

def test_scan_existing_walks_top_level_drafts_dir(email_root: Path) -> None:
    drafts = email_root / "drafts"
    a = _write_draft(drafts, "a", {"from": "x@x.com", "to": ["y@y.com"]})
    b = _write_draft(drafts, "b", {"from": "x@x.com", "to": ["y@y.com"]})
    old = email_root / "user@example.com" / "drafts" / "old.yaml"
    old.parent.mkdir(parents=True)
    old.touch()

    found = set(outbound._scan_existing())
    assert found == {a, b}
