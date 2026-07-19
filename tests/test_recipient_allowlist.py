"""recipient_allowlist — matching rules for send_allowlist (fail-closed)."""

from pathlib import Path

import pytest
import yaml

from boot import config
from boot import recipient_allowlist as ra


# ---------- normalize_phone -------------------------------------------------

def test_normalize_phone_strips_formatting():
    assert ra.normalize_phone("+1 (555) 123-4567") == "15551234567"
    assert ra.normalize_phone("+15551234567") == "15551234567"


def test_normalize_phone_rejects_non_phone():
    assert ra.normalize_phone("bob@corp.com") is None
    assert ra.normalize_phone("iMessage;+;chat123") is None
    assert ra.normalize_phone("") is None
    assert ra.normalize_phone("123") is None  # too short to be a phone


# ---------- handle_allowed (imessage / whatsapp) ----------------------------

def test_phone_rule_matches_formatted_variants():
    rules = ["+1 555-123-4567"]
    assert ra.handle_allowed("+15551234567", rules)
    assert ra.handle_allowed("15551234567@s.whatsapp.net", rules)


def test_phone_rule_no_country_code_guessing():
    # Fail-closed: "5551234567" is not "+15551234567".
    assert not ra.handle_allowed("+15551234567", ["555-123-4567"])


def test_email_handle_matches_case_insensitive():
    assert ra.handle_allowed("Bob@Corp.com", ["bob@corp.com"])


def test_chat_guid_exact_match():
    guid = "iMessage;+;chat443533398519855587"
    assert ra.handle_allowed(guid, [guid])
    assert not ra.handle_allowed(guid, ["iMessage;+;chat999"])


def test_whatsapp_group_jid_exact_only():
    gid = "120363123456789@g.us"
    assert ra.handle_allowed(gid, [gid])
    assert not ra.handle_allowed(gid, ["120363123456789"])


def test_handle_fail_closed_on_empty():
    assert not ra.handle_allowed("", ["+15551234567"])
    assert not ra.handle_allowed("+15551234567", [])
    assert not ra.handle_allowed("+15551234567", ["", "   "])


# ---------- emails_allowed --------------------------------------------------

def test_email_exact_and_wildcard():
    rules = ["premomtx@gmail.com", "*@corp.com"]
    assert ra.emails_allowed(["premomtx@gmail.com"], rules)
    assert ra.emails_allowed(["Alice@corp.com", "bob@CORP.com"], rules)


def test_email_all_recipients_must_match():
    rules = ["*@corp.com"]
    assert not ra.emails_allowed(["alice@corp.com", "eve@other.com"], rules)


def test_email_wildcard_no_subdomains():
    assert not ra.emails_allowed(["a@mail.corp.com"], ["*@corp.com"])


def test_email_name_angle_form_extracted():
    assert ra.emails_allowed(["Bob Smith <bob@corp.com>"], ["*@corp.com"])


def test_email_fail_closed():
    assert not ra.emails_allowed([], ["*@corp.com"])       # no recipients
    assert not ra.emails_allowed(["a@corp.com"], [])       # no rules
    assert not ra.emails_allowed(["not-an-address"], ["*@corp.com"])
    assert not ra.emails_allowed(["a@corp.com"], ["*@"])   # malformed rule


# ---------- config plumbing (send_allowlist:) -------------------------------

@pytest.fixture
def cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    p = tmp_path / "etc" / "config.yaml"
    p.parent.mkdir(parents=True)
    p.write_text("pais: []\n")
    monkeypatch.setattr(config, "CONFIG_PATH", p, raising=True)
    return p


def test_send_allowlist_roundtrip_and_dedupe(cfg: Path) -> None:
    assert config.send_allowlist("imessage") == []
    config.set_send_allowlist("imessage", ["+1555", "a@b.co", "+1555"])
    assert config.send_allowlist("imessage") == ["+1555", "a@b.co"]
    assert config.send_allowlist("whatsapp") == []  # channels independent
    config.set_send_allowlist("imessage", [])
    assert config.send_allowlist("imessage") == []
    assert "send_allowlist" not in (yaml.safe_load(cfg.read_text()) or {})


def test_send_allowlist_unknown_channel(cfg: Path) -> None:
    assert config.send_allowlist("carrier-pigeon") == []
    with pytest.raises(ValueError):
        config.set_send_allowlist("carrier-pigeon", ["x"])


def test_set_send_allowlist_rejects_blank_rules(cfg: Path) -> None:
    with pytest.raises(ValueError):
        config.set_send_allowlist("email", ["  "])


def test_send_allowlist_tolerates_malformed_map(cfg: Path) -> None:
    cfg.write_text("pais: []\nsend_allowlist: nonsense\n")
    assert config.send_allowlist("email") == []
    cfg.write_text("pais: []\nsend_allowlist:\n  email: nonsense\n")
    assert config.send_allowlist("email") == []
