from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from boot import paths
from usr.libexec.web.pai_web import actions


@pytest.fixture
def queue(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "pai"
    monkeypatch.setattr(paths, "PAI_ROOT", root, raising=True)
    q = paths.var_spool_approvals()
    q.mkdir(parents=True, exist_ok=True)
    return q


def _write(queue: Path, ident: str, **over) -> Path:
    rec = {
        "id": ident,
        "channel": "email",
        "status": "pending",
        "created_by": "email-pai",
        "created_at": "2026-06-30T09:00:00",
        "action": {
            "from": "me@x.com",
            "to": ["bob@acme.com"],
            "cc": [],
            "subject": "Re: test",
            "content": "Hi Bob,\n\nthanks.",
        },
        "decided_at": None,
        "decided_by": None,
        "error": None,
    }
    rec.update(over)
    path = queue / f"{ident}.yaml"
    path.write_text(yaml.safe_dump(rec, sort_keys=False), encoding="utf-8")
    return path


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_list_pending_projects_review_subset(queue: Path) -> None:
    _write(queue, "20260630-090000-bob")
    [item] = actions.list_pending()
    # Only the review fields, nothing more (no action/provenance/token).
    assert item == {
        "id": "20260630-090000-bob",
        "channel": "email",
        "created_by": "email-pai",
        "created_at": "2026-06-30T09:00:00",
        "recipient": "bob@acme.com",
        "subject": "Re: test",
        "body": "Hi Bob,\n\nthanks.",
        # What "Approve & always allow" would grant, verbatim in the button.
        "allow_rules": ["bob@acme.com"],
    }


def test_list_pending_omits_decided_records(queue: Path) -> None:
    _write(queue, "a-pending")
    _write(queue, "b-approved", status="approved")
    _write(queue, "c-rejected", status="rejected")
    _write(queue, "d-dispatched", status="dispatched")
    assert [i["id"] for i in actions.list_pending()] == ["a-pending"]


def test_list_pending_sorted_by_created_at(queue: Path) -> None:
    _write(queue, "late", created_at="2026-06-30T12:00:00")
    _write(queue, "early", created_at="2026-06-30T08:00:00")
    assert [i["id"] for i in actions.list_pending()] == ["early", "late"]


def test_list_pending_recipient_falls_back_to_in_reply_to(queue: Path) -> None:
    _write(
        queue,
        "reply",
        action={
            "from": "me@x.com",
            "to": [],
            "cc": [],
            "subject": "Re: thread",
            "in_reply_to": "<msg-123@acme.com>",
            "content": "ok",
        },
    )
    [item] = actions.list_pending()
    assert item["recipient"] == "<msg-123@acme.com>"


def test_approve_flips_only_pending(queue: Path) -> None:
    path = _write(queue, "x")
    assert actions.approve_action("x") == {"id": "x", "status": "approved"}
    rec = _load(path)
    assert rec["status"] == "approved"
    assert rec["decided_by"] == "owner"
    assert rec["decided_at"]


def test_approve_with_body_override_merges_edited_content(queue: Path) -> None:
    path = _write(queue, "x")
    assert actions.approve_action("x", body_override="edited body") == {
        "id": "x", "status": "approved",
    }
    rec = _load(path)
    assert rec["action"]["content"] == "edited body"
    # Nothing else in the action changed.
    assert rec["action"]["to"] == ["bob@acme.com"]


def test_approve_with_body_override_imessage_uses_text_key(queue: Path) -> None:
    path = _write(
        queue, "x", channel="imessage",
        action={"thread": "bob", "text": "original"},
    )
    actions.approve_action("x", body_override="edited")
    rec = _load(path)
    assert rec["action"]["text"] == "edited"


def test_approve_guards_already_decided(queue: Path) -> None:
    path = _write(queue, "x", status="approved", decided_by="owner")
    out = actions.approve_action("x")
    assert out == {"id": "x", "status": "approved", "error": "not pending"}
    # The terminal-guard must not rewrite/re-stamp the record.
    assert _load(path)["decided_by"] == "owner"


def test_reject_sets_reason_and_status(queue: Path) -> None:
    path = _write(queue, "x")
    assert actions.reject_action("x", "wrong recipient") == {"id": "x", "status": "rejected"}
    rec = _load(path)
    assert rec["status"] == "rejected"
    assert rec["error"] == "wrong recipient"
    assert rec["decided_by"] == "owner"


def test_reject_blank_reason_is_none(queue: Path) -> None:
    path = _write(queue, "x")
    actions.reject_action("x", "")
    assert _load(path)["error"] is None


def test_decide_missing_record(queue: Path) -> None:
    assert actions.approve_action("ghost")["error"] == "not found"


@pytest.mark.parametrize("bad", ["../escape", "a/b", "..", "", "foo/../bar"])
def test_path_traversal_id_rejected(queue: Path, bad: str) -> None:
    with pytest.raises(ValueError, match="invalid approval id"):
        actions.approve_action(bad)
    with pytest.raises(ValueError, match="invalid approval id"):
        actions.reject_action(bad)


# --- approve & always allow -------------------------------------------------


@pytest.fixture
def cfg(queue: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from boot import config

    p = tmp_path / "pai" / "etc" / "config.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("pais: []\n")
    monkeypatch.setattr(config, "CONFIG_PATH", p, raising=True)
    return p


def test_always_allow_email_appends_recipients_and_approves(queue: Path, cfg: Path) -> None:
    from boot import config

    path = _write(queue, "e1")
    out = actions.approve_action("e1", always_allow=True)
    assert out["status"] == "approved"
    assert _load(path)["status"] == "approved"
    assert config.send_allowlist("email") == ["bob@acme.com"]


def test_email_reply_offers_no_allow_rule(queue: Path, cfg: Path) -> None:
    from boot import config

    _write(
        queue, "e2",
        action={"from": "me@x.com", "to": ["bob@acme.com"],
                "in_reply_to": "<id@acme.com>", "content": "hi"},
    )
    [item] = actions.list_pending()
    assert item["allow_rules"] == []
    # always_allow on it approves but grants nothing.
    actions.approve_action("e2", always_allow=True)
    assert config.send_allowlist("email") == []


def test_always_allow_bash_uses_edited_command(queue: Path, cfg: Path) -> None:
    from boot import config

    _write(queue, "b1", channel="bash", action={"command": "git push origin main", "tool": "bash"})
    actions.approve_action("b1", body_override="git push", always_allow=True)
    assert config.bash_allowlist() == ["git push"]


def test_always_allow_imessage_resolves_thread_handle(queue: Path, cfg: Path) -> None:
    from boot import config

    thread = paths.var_spool_messages() / "habib"
    thread.mkdir(parents=True)
    (thread / "meta.yaml").write_text(
        yaml.safe_dump({"channel": "imessage", "handles": ["+15551234567"]})
    )
    _write(queue, "m1", channel="imessage", action={"thread": "habib", "text": "hi"})
    [item] = actions.list_pending()
    assert item["allow_rules"] == ["+15551234567"]
    actions.approve_action("m1", always_allow=True)
    assert config.send_allowlist("imessage") == ["+15551234567"]


def test_always_allow_unresolvable_thread_grants_nothing(queue: Path, cfg: Path) -> None:
    from boot import config

    _write(queue, "m2", channel="imessage", action={"thread": "ghost", "text": "hi"})
    [item] = actions.list_pending()
    assert item["allow_rules"] == []
    actions.approve_action("m2", always_allow=True)
    assert config.send_allowlist("imessage") == []
