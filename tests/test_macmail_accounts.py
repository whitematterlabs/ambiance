"""macmail accounts module — discovery parsing, persistence, lookups.

osascript itself is mocked; these tests never shell out to Mail.app.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from drivers.email.macmail import accounts as A


# ---------- parse_discovery_output ----------------------------------------

def test_parse_discovery_groups_addresses_per_account() -> None:
    text = (
        "ADDR|UUID-A|primary@example.com\n"
        "ADDR|UUID-A|alias@example.com\n"
        "ADDR|UUID-B|other@example.com\n"
        "INBOX|UUID-A|INBOX\n"
        "SENT|UUID-A|Sent Messages\n"
        "INBOX|UUID-B|Gelen Kutusu\n"
        "SENT|UUID-B|Gönderilmiş Öğeler\n"
    )
    cfg = A.parse_discovery_output(text)
    assert set(cfg.accounts) == {"UUID-A", "UUID-B"}
    a = cfg.accounts["UUID-A"]
    assert a.addresses == ["primary@example.com", "alias@example.com"]
    assert a.inbox_name == "INBOX"
    assert a.sent_name == "Sent Messages"
    b = cfg.accounts["UUID-B"]
    assert b.inbox_name == "Gelen Kutusu"
    assert b.sent_name == "Gönderilmiş Öğeler"


def test_parse_discovery_ignores_blank_and_malformed_lines() -> None:
    text = (
        "\n"
        "garbage with no pipes\n"
        "ADDR|UUID|x@y.com\n"
        "ONLY|TWO\n"
        "INBOX|UUID|INBOX\n"
    )
    cfg = A.parse_discovery_output(text)
    assert "UUID" in cfg.accounts
    assert cfg.accounts["UUID"].addresses == ["x@y.com"]


def test_parse_discovery_skips_blank_uuid_or_value() -> None:
    text = "ADDR||x@y.com\nADDR|UUID|\nINBOX|UUID|\n"
    cfg = A.parse_discovery_output(text)
    assert cfg.is_empty()


def test_parse_discovery_dedupes_addresses() -> None:
    text = "ADDR|U|x@y.com\nADDR|U|x@y.com\n"
    cfg = A.parse_discovery_output(text)
    assert cfg.accounts["U"].addresses == ["x@y.com"]


# ---------- AccountsConfig lookups ----------------------------------------

def test_address_for_uuid_returns_primary() -> None:
    cfg = A.parse_discovery_output(
        "ADDR|U|primary@x.com\nADDR|U|alias@x.com\n"
    )
    assert cfg.address_for_uuid("U") == "primary@x.com"


def test_address_for_uuid_unknown_is_none() -> None:
    assert A.AccountsConfig().address_for_uuid("nope") is None


def test_accepts_from_includes_aliases_case_insensitive() -> None:
    cfg = A.parse_discovery_output(
        "ADDR|U|Primary@Example.com\nADDR|U|alias@privaterelay.appleid.com\n"
    )
    assert cfg.accepts_from("primary@example.com")
    assert cfg.accepts_from("PRIMARY@example.com")
    assert cfg.accepts_from("alias@privaterelay.appleid.com")
    assert not cfg.accepts_from("stranger@nowhere.com")
    assert not cfg.accepts_from("")


def test_all_addresses_lowercased_unique() -> None:
    cfg = A.parse_discovery_output(
        "ADDR|U1|A@x.com\nADDR|U1|A@x.com\nADDR|U2|b@x.com\n"
    )
    assert cfg.all_addresses() == ["a@x.com", "b@x.com"]


# ---------- url_like_patterns / role_for_url -------------------------------

def test_url_like_patterns_uses_uuid_and_encoded_name() -> None:
    cfg = A.parse_discovery_output(
        "ADDR|UUID-X|x@y.com\nINBOX|UUID-X|Gelen Kutusu\nSENT|UUID-X|Sent Messages\n"
    )
    pats = dict((role, pat) for pat, role in cfg.url_like_patterns())
    assert pats["inbound"] == "%UUID-X%/Gelen%20Kutusu"
    assert pats["outbound"] == "%UUID-X%/Sent%20Messages"


def test_url_like_patterns_normalizes_nfd_for_diacritics() -> None:
    """Mail.app's Envelope Index URL-encodes mailbox names from NFD bytes
    (Apple convention). The pattern must use the same normalization or it
    won't match."""
    cfg = A.parse_discovery_output("INBOX|U|Gönderilmiş Öğeler\n")
    pat = cfg.url_like_patterns()[0][0]
    # Combining diaeresis + cedilla + breve are encoded individually.
    assert "%CC%88" in pat  # combining diaeresis
    assert "%CC%A7" in pat  # combining cedilla
    assert "%CC%86" in pat  # combining breve


def test_role_for_url_classifies_inbound_and_outbound() -> None:
    cfg = A.parse_discovery_output(
        "ADDR|0A836680|x@y.com\n"
        "INBOX|0A836680|INBOX\n"
        "SENT|0A836680|Sent Messages\n"
        "ADDR|BFFD063A|out@y.com\n"
        "INBOX|BFFD063A|Gelen Kutusu\n"
        "SENT|BFFD063A|Gönderilmiş Öğeler\n"
    )
    assert cfg.role_for_url("imap://0A836680-D0A5/INBOX") == "inbound"
    assert cfg.role_for_url("imap://0A836680-D0A5/Sent%20Messages") == "outbound"
    assert cfg.role_for_url("ews://BFFD063A-FC47/Gelen%20Kutusu") == "inbound"
    assert cfg.role_for_url("imap://0A836680-D0A5/Junk") is None
    assert cfg.role_for_url("imap://OTHER-UUID/INBOX") is None


def test_role_for_url_handles_subfoldered_sent() -> None:
    """Gmail's sent mailbox URL is `[Gmail]/Sent Mail`; AppleScript reports
    the basename `Sent Mail`. The pattern should still match."""
    cfg = A.parse_discovery_output(
        "ADDR|GMAIL|x@gmail.com\nSENT|GMAIL|Sent Mail\n"
    )
    assert cfg.role_for_url("imap://GMAIL-UUID/%5BGmail%5D/Sent%20Mail") == "outbound"


# ---------- persistence ---------------------------------------------------

def test_load_returns_empty_when_file_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(A, "ACCOUNTS_PATH", tmp_path / "accounts.yaml", raising=True)
    assert A.load().is_empty()


def test_load_discards_old_flat_schema(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Old format was `{uuid: address}` at top level — should round-trip
    to empty so refresh() repopulates from Mail.app."""
    p = tmp_path / "accounts.yaml"
    p.write_text("UUID-OLD: stale@relay.appleid.com\n")
    monkeypatch.setattr(A, "ACCOUNTS_PATH", p, raising=True)
    assert A.load().is_empty()


def test_load_reads_new_schema_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = tmp_path / "accounts.yaml"
    p.write_text(
        yaml.safe_dump({
            "accounts": {
                "U": {
                    "addresses": ["a@x.com", "b@x.com"],
                    "inbox_name": "INBOX",
                    "sent_name": "Sent Messages",
                }
            }
        })
    )
    monkeypatch.setattr(A, "ACCOUNTS_PATH", p, raising=True)
    cfg = A.load()
    assert cfg.address_for_uuid("U") == "a@x.com"
    assert cfg.accepts_from("b@x.com")
    inbound_pat = next(pat for pat, role in cfg.url_like_patterns() if role == "inbound")
    assert inbound_pat.endswith("/INBOX")


def test_refresh_persists_and_returns_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """refresh() runs osascript, parses, writes the file, returns the cfg."""
    monkeypatch.setattr(A, "ACCOUNTS_PATH", tmp_path / "accounts.yaml", raising=True)

    canned = (
        "ADDR|U1|primary@example.com\n"
        "INBOX|U1|INBOX\n"
        "SENT|U1|Sent Messages\n"
    )

    async def fake_run(script: str) -> tuple[int, str, str]:
        return (0, canned, "")

    monkeypatch.setattr(A, "_run_osascript", fake_run)

    import asyncio
    cfg = asyncio.run(A.refresh())
    assert cfg.address_for_uuid("U1") == "primary@example.com"

    # Round-trip from the persisted file matches.
    persisted = A.load()
    assert persisted.accounts == cfg.accounts


def test_refresh_falls_back_to_persisted_on_osascript_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = tmp_path / "accounts.yaml"
    p.write_text(
        yaml.safe_dump({
            "accounts": {
                "U": {
                    "addresses": ["existing@x.com"],
                    "inbox_name": "INBOX",
                    "sent_name": "Sent Messages",
                }
            }
        })
    )
    monkeypatch.setattr(A, "ACCOUNTS_PATH", p, raising=True)

    async def fake_run(script: str) -> tuple[int, str, str]:
        return (1, "", "automation denied")

    monkeypatch.setattr(A, "_run_osascript", fake_run)

    import asyncio
    cfg = asyncio.run(A.refresh())
    # File preserved; cached config returned.
    assert cfg.address_for_uuid("U") == "existing@x.com"
