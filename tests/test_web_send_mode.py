"""Tests for the sidebar send-permission control (web surface).

Covers the two write/read helpers the toggle relies on: `set_send_mode`
(persist + reload) and `list_send_capabilities` (mounted-channel projection).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from boot import config as C
from usr.libexec.web.pai_web import actions


_BODY = """
capabilities:
  email_send: off
  imessage_send: approve
pais:
  - name: root
    pid: 1
    description: km
"""


def _write_config(repo_root: Path, body: str = _BODY) -> Path:
    path = repo_root / "etc" / "config.yaml"
    path.write_text(body)
    return path


def test_set_send_mode_persists_and_emits(repo_root, tmp_path):
    _write_config(repo_root)
    out = actions.set_send_mode("email_send", "auto")
    assert out == {"flag": "email_send", "mode": "auto"}
    # The choice landed in config.yaml (read back through the normal accessor).
    assert C.capability_modes()["email_send"] == "auto"
    # A kernel:reload_config event was queued so the freeze re-projects live.
    events = list((tmp_path / "events").glob("*.yaml"))
    assert events, "expected a reload event file"


def test_set_send_mode_rejects_bad_mode(repo_root):
    path = _write_config(repo_root)
    with pytest.raises(ValueError):
        actions.set_send_mode("email_send", "nope")
    # The rejected write leaves the file untouched.
    assert C.capability_modes()["email_send"] == "off"
    assert path.read_text()  # still there, unchanged


def test_set_send_mode_rejects_unknown_flag(repo_root):
    _write_config(repo_root)
    with pytest.raises(ValueError):
        actions.set_send_mode("sms_send", "auto")


def test_list_send_capabilities_filters_unmounted(repo_root, monkeypatch):
    _write_config(repo_root)
    # Only the email driver is mounted → only Email shows, at its live mode.
    monkeypatch.setattr(actions, "_mounted_driver_union", lambda: {"email"})
    assert actions.list_send_capabilities() == [
        {"flag": "email_send", "channel": "Email", "mode": "off"},
    ]


def test_list_send_capabilities_empty_when_nothing_mounted(repo_root, monkeypatch):
    _write_config(repo_root)
    monkeypatch.setattr(actions, "_mounted_driver_union", lambda: set())
    assert actions.list_send_capabilities() == []


def test_list_send_capabilities_reports_both_channels(repo_root, monkeypatch):
    _write_config(repo_root)
    monkeypatch.setattr(
        actions, "_mounted_driver_union", lambda: {"email", "imessage"}
    )
    assert actions.list_send_capabilities() == [
        {"flag": "email_send", "channel": "Email", "mode": "off"},
        {"flag": "imessage_send", "channel": "iMessage", "mode": "approve"},
    ]
