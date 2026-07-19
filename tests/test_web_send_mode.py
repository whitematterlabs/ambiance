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
  email_send: no
  imessage_send: ask
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
    out = actions.set_send_mode("email_send", "yes")
    assert out == {"flag": "email_send", "mode": "yes"}
    # The choice landed in config.yaml (read back through the normal accessor).
    assert C.capability_modes()["email_send"] == "yes"
    # A kernel:reload_config event was queued so the freeze re-projects live.
    events = list((tmp_path / "events").glob("*.yaml"))
    assert events, "expected a reload event file"


def test_set_send_mode_rejects_bad_mode(repo_root):
    path = _write_config(repo_root)
    with pytest.raises(ValueError):
        actions.set_send_mode("email_send", "nope")
    # The rejected write leaves the file untouched.
    assert C.capability_modes()["email_send"] == "no"
    assert path.read_text()  # still there, unchanged


def test_set_send_mode_rejects_unknown_flag(repo_root):
    _write_config(repo_root)
    with pytest.raises(ValueError):
        actions.set_send_mode("sms_send", "yes")


_SEND_MODES = ["no", "ask", "yes"]

# bash_exec is kernel-enforced (driver: None) — always listed, regardless of
# mounted drivers, with the allowlist riding on the row. Send rows carry
# their send_allowlist the same way.
_BASH_ROW = {"flag": "bash_exec", "channel": "Shell commands", "mode": "yes", "modes": _SEND_MODES, "allowlist": []}



def test_list_send_capabilities_filters_unmounted(repo_root, monkeypatch):
    _write_config(repo_root)
    # Only the email driver is mounted → only Email shows, at its live mode.
    monkeypatch.setattr(actions, "_mounted_driver_union", lambda: {"email"})
    assert actions.list_send_capabilities() == [
        {"flag": "email_send", "channel": "Email", "mode": "no", "modes": _SEND_MODES, "allowlist": []},
        _BASH_ROW,
    ]


def test_list_send_capabilities_kernel_gates_only_when_nothing_mounted(repo_root, monkeypatch):
    _write_config(repo_root)
    monkeypatch.setattr(actions, "_mounted_driver_union", lambda: set())
    # Kernel-enforced gates still show — they apply to every PAI.
    assert actions.list_send_capabilities() == [_BASH_ROW]


def test_list_send_capabilities_reports_both_channels(repo_root, monkeypatch):
    _write_config(repo_root)
    monkeypatch.setattr(
        actions, "_mounted_driver_union", lambda: {"email", "imessage"}
    )
    assert actions.list_send_capabilities() == [
        {"flag": "email_send", "channel": "Email", "mode": "no", "modes": _SEND_MODES, "allowlist": []},
        {"flag": "imessage_send", "channel": "iMessage", "mode": "ask", "modes": _SEND_MODES, "allowlist": []},
        _BASH_ROW,
    ]


def test_list_send_capabilities_capture_gate_rows(repo_root, monkeypatch):
    # Capture gates are two-state and the cowork facets default on — each row
    # must say so, so the frontend renders a two-button toggle, not a dead
    # "Ask". One row per facet: the whole point of the split.
    _write_config(repo_root)
    monkeypatch.setattr(actions, "_mounted_driver_union", lambda: {"cowork"})
    assert actions.list_send_capabilities() == [
        {"flag": "cowork_window", "channel": "Windows & tabs", "mode": "yes", "modes": ["no", "yes"]},
        {"flag": "cowork_clipboard", "channel": "Clipboard", "mode": "yes", "modes": ["no", "yes"]},
        {"flag": "cowork_files", "channel": "File activity", "mode": "yes", "modes": ["no", "yes"]},
        _BASH_ROW,
    ]
