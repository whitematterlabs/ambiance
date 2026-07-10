"""Tests for src/kernel/config.py — load, validate, reconcile."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from bin.paifs_init import default_config_yaml
from boot import config as C
from boot import paths as PA
from boot import processes as P


def _write_config(repo_root: Path, body: str) -> Path:
    path = repo_root / "etc" / "config.yaml"
    path.write_text(body)
    return path


def _write_package(repo_root: Path, name: str, body: dict) -> Path:
    pkg_dir = repo_root / "packages" / name
    pkg_dir.mkdir(parents=True, exist_ok=True)
    path = pkg_dir / "package.yaml"
    with path.open("w") as f:
        yaml.safe_dump(body, f)
    return path


# ----- load_config / validation -----


def test_load_minimal(repo_root):
    _write_config(
        repo_root,
        """
pais:
  - name: root
    pid: 1
    description: km
  - name: pai
    pid: 2
    description: dflt
""",
    )
    cfg = C.load_config()
    assert set(cfg) == {"root", "pai"}
    assert cfg["root"]["pid"] == 1
    assert cfg["pai"]["description"] == "dflt"


def test_missing_file(repo_root):
    with pytest.raises(C.ConfigError, match="not found"):
        C.load_config()


# ----- onboarding_pending / clear_onboarding_pending -----


_ONBOARDING_BODY = """
onboarding_pending: true
pais:
  - name: root
    pid: 1
    description: km
  - name: pai
    pid: 2
    description: dflt
    fallback: true
"""


def test_onboarding_pending_true(repo_root):
    path = _write_config(repo_root, _ONBOARDING_BODY)
    assert C.onboarding_pending(path) is True


def test_onboarding_pending_absent_is_false(repo_root):
    path = _write_config(
        repo_root,
        """
pais:
  - name: pai
    pid: 2
    description: dflt
""",
    )
    assert C.onboarding_pending(path) is False


def test_onboarding_pending_missing_file_is_false(repo_root):
    # Tolerant: no config at all → False, no raise.
    assert C.onboarding_pending(repo_root / "etc" / "nope.yaml") is False


def test_clear_onboarding_pending_flips_flag_and_preserves_pais(repo_root):
    path = _write_config(repo_root, _ONBOARDING_BODY)
    C.clear_onboarding_pending(path)
    assert C.onboarding_pending(path) is False
    # The fleet survives the rewrite untouched.
    raw = yaml.safe_load(path.read_text())
    assert raw["onboarding_pending"] is False
    assert [e["name"] for e in raw["pais"]] == ["root", "pai"]
    # And load_config still parses it cleanly — flag is inert to reconcile.
    cfg = C.load_config(path)
    assert set(cfg) == {"root", "pai"}


def test_clear_onboarding_pending_missing_file_is_noop(repo_root):
    # No file → nothing to clear, no raise.
    C.clear_onboarding_pending(repo_root / "etc" / "nope.yaml")


def test_duplicate_name(repo_root):
    _write_config(
        repo_root,
        """
pais:
  - name: root
    pid: 1
    description: a
  - name: root
    pid: 2
    description: b
""",
    )
    with pytest.raises(C.ConfigError, match="duplicate name"):
        C.load_config()


def test_duplicate_pid(repo_root):
    _write_config(
        repo_root,
        """
pais:
  - name: root
    pid: 1
    description: a
  - name: pai
    pid: 1
    description: b
""",
    )
    with pytest.raises(C.ConfigError, match="reserved for"):
        C.load_config()


def test_reserved_pid_wrong_name(repo_root):
    _write_config(
        repo_root,
        """
pais:
  - name: not_root
    pid: 1
    description: nope
""",
    )
    with pytest.raises(C.ConfigError, match="reserved for 'root'"):
        C.load_config()


def test_reserved_name_wrong_pid(repo_root):
    _write_config(
        repo_root,
        """
pais:
  - name: root
    pid: 99
    description: oops
""",
    )
    with pytest.raises(C.ConfigError, match="reserved entry"):
        C.load_config()


def test_missing_description(repo_root):
    _write_config(
        repo_root,
        """
pais:
  - name: root
    pid: 1
""",
    )
    with pytest.raises(C.ConfigError, match="description"):
        C.load_config()


def test_provider_unknown(repo_root):
    _write_config(
        repo_root,
        """
pais:
  - name: root
    pid: 1
    description: km
    provider: not-a-provider
""",
    )
    with pytest.raises(C.ConfigError, match="unknown provider"):
        C.load_config()


def test_provider_openai_accepted(repo_root, live_dir):
    # Regression guard for the openai row: config must accept `provider: openai`
    # now that it exists in L.PROVIDERS, and persist it to spec.yaml.
    _write_config(
        repo_root,
        """
pais:
  - name: root
    pid: 1
    description: km
    provider: openai
    model: gpt-5.5
""",
    )
    C.reconcile_from_config()
    spec = P.read_spec("root")
    assert spec["provider"] == "openai"
    assert spec["model"] == "gpt-5.5"


def test_provider_persisted(repo_root, live_dir):
    _write_config(
        repo_root,
        """
pais:
  - name: root
    pid: 1
    description: km
    provider: deepseek
    model: deepseek-v4-pro
  - name: pai
    pid: 2
    description: dflt
""",
    )
    C.reconcile_from_config()
    spec = P.read_spec("root")
    assert spec["provider"] == "deepseek"
    assert spec["model"] == "deepseek-v4-pro"


def test_wake_on_type(repo_root):
    _write_config(
        repo_root,
        """
pais:
  - name: root
    pid: 1
    description: km
    wake_on: "not-a-list"
""",
    )
    with pytest.raises(C.ConfigError, match="wake_on"):
        C.load_config()


# ----- package merge -----


def test_package_merge(repo_root):
    _write_package(
        repo_root,
        "msg_spec",
        {
            "kind": "pai",
            "name": "message_specialist",
            "description": "from package",
            "model": "deepseek-v4-pro",
            "prompt": "prompt.md",
            "wake_on": ["imessage:*"],
        },
    )
    _write_config(
        repo_root,
        """
pais:
  - name: root
    pid: 1
    description: km
  - name: pai
    pid: 2
    description: dflt
  - name: msg
    package: msg_spec
    description: inline-override
""",
    )
    cfg = C.load_config()
    msg = cfg["msg"]
    # inline `description` overrides package
    assert msg["description"] == "inline-override"
    # package fields flow through
    assert msg["model"] == "deepseek-v4-pro"
    assert msg["wake_on"] == ["imessage:*"]


def test_seed_config_overrides_librarian_package_provider(repo_root):
    for name in ("owner", "memory-usage", "capability-escalation"):
        p = repo_root / "etc" / "boilerplate" / f"{name}.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"# {name}\n")
    _write_package(
        repo_root,
        "librarian",
        {
            "kind": "pai",
            "name": "librarian",
            "description": "from package",
            "provider": "deepseek",
            "model": "deepseek-v4-pro",
            "wake_on": ["librarian:consolidate"],
        },
    )
    _write_config(
        repo_root,
        default_config_yaml(provider="openai", model="gpt-5.5"),
    )

    cfg = C.load_config()
    assert cfg["librarian"]["provider"] == "openai"
    assert cfg["librarian"]["model"] == "gpt-5.5"


def test_package_kind_unsupported(repo_root):
    _write_package(repo_root, "skill_pkg", {"kind": "skill", "name": "x", "description": "y"})
    _write_config(
        repo_root,
        """
pais:
  - name: root
    pid: 1
    description: km
  - name: pai
    pid: 2
    description: dflt
  - name: x
    package: skill_pkg
    description: huh
""",
    )
    with pytest.raises(NotImplementedError, match="skill"):
        C.load_config()


# ----- reconcile -----


def _seed_etc(repo_root: Path, body: str) -> None:
    _write_config(repo_root, body)


def test_reconcile_cold_boot(repo_root, live_dir):
    _seed_etc(
        repo_root,
        """
pais:
  - name: root
    pid: 1
    description: km
    prompt: src/prompts/root.md
    model: deepseek-v4-pro
    wake_on: ['kernel:*']
  - name: pai
    pid: 2
    description: dflt
    prompt: src/prompts/pai_default.md
    model: deepseek-v4-pro
    wake_on: ['*']
""",
    )
    C.reconcile_from_config()
    actual = dict(P._iter_pai_specs())
    assert set(actual) == {"root", "pai"}
    assert actual["root"]["pid"] == 1
    assert actual["pai"]["pid"] == 2
    assert actual["pai"]["wake_on"] == ["*"]
    assert P.read_status("root") == "running"


def test_reconcile_add(repo_root, live_dir):
    # First reconcile: just the reserved pair.
    _seed_etc(
        repo_root,
        """
pais:
  - name: root
    pid: 1
    description: km
  - name: pai
    pid: 2
    description: dflt
""",
    )
    C.reconcile_from_config()
    # Now add a third entry without a pid.
    _seed_etc(
        repo_root,
        """
pais:
  - name: root
    pid: 1
    description: km
  - name: pai
    pid: 2
    description: dflt
  - name: extra
    description: a third one
""",
    )
    C.reconcile_from_config()
    actual = dict(P._iter_pai_specs())
    assert "extra" in actual
    assert actual["extra"]["pid"] >= 3


def test_reconcile_remove(repo_root, live_dir):
    _seed_etc(
        repo_root,
        """
pais:
  - name: root
    pid: 1
    description: km
  - name: pai
    pid: 2
    description: dflt
  - name: extra
    pid: 5
    description: temp
""",
    )
    C.reconcile_from_config()
    assert P.read_status("extra") == "running"
    # Remove extra.
    _seed_etc(
        repo_root,
        """
pais:
  - name: root
    pid: 1
    description: km
  - name: pai
    pid: 2
    description: dflt
""",
    )
    C.reconcile_from_config()
    # Proc dir preserved on disk; status flipped to cancelled.
    assert (live_dir / "proc" / "extra").exists()
    assert P.read_status("extra") == "cancelled"


def test_reconcile_change_rewrites_spec(repo_root, live_dir):
    _seed_etc(
        repo_root,
        """
pais:
  - name: root
    pid: 1
    description: original
  - name: pai
    pid: 2
    description: dflt
""",
    )
    C.reconcile_from_config()
    _seed_etc(
        repo_root,
        """
pais:
  - name: root
    pid: 1
    description: updated
    wake_on: ['kernel:*']
  - name: pai
    pid: 2
    description: dflt
""",
    )
    C.reconcile_from_config()
    spec = P.read_spec("root")
    assert spec["description"] == "updated"
    assert spec["wake_on"] == ["kernel:*"]
    assert spec["pid"] == 1  # unchanged


def test_reconcile_pid_invariant(repo_root, live_dir):
    _seed_etc(
        repo_root,
        """
pais:
  - name: root
    pid: 1
    description: km
  - name: pai
    pid: 2
    description: dflt
  - name: extra
    pid: 5
    description: temp
""",
    )
    C.reconcile_from_config()
    # Try to change extra's pid — should fail before any mutation.
    _seed_etc(
        repo_root,
        """
pais:
  - name: root
    pid: 1
    description: km
  - name: pai
    pid: 2
    description: dflt
  - name: extra
    pid: 9
    description: temp
""",
    )
    with pytest.raises(C.ConfigError, match="cannot change"):
        C.reconcile_from_config()
    assert P.read_spec("extra")["pid"] == 5


def test_reconcile_preserves_unmanaged_fields(repo_root, live_dir):
    _seed_etc(
        repo_root,
        """
pais:
  - name: root
    pid: 1
    description: km
  - name: pai
    pid: 2
    description: dflt
""",
    )
    C.reconcile_from_config()
    # Manually set an unmanaged field.
    spec_path = live_dir / "proc" / "root" / "spec.yaml"
    with spec_path.open() as f:
        spec = yaml.safe_load(f)
    spec["persistent"] = True
    with spec_path.open("w") as f:
        yaml.safe_dump(spec, f)
    # Reconcile with a change to a managed field.
    _seed_etc(
        repo_root,
        """
pais:
  - name: root
    pid: 1
    description: changed
  - name: pai
    pid: 2
    description: dflt
""",
    )
    C.reconcile_from_config()
    spec = P.read_spec("root")
    assert spec["persistent"] is True
    assert spec["description"] == "changed"


# ----- capabilities: flags + freeze projection -----


def test_capability_flags_default_deny_when_absent(repo_root):
    _write_config(
        repo_root,
        """
pais:
  - name: root
    pid: 1
    description: km
""",
    )
    flags = C.capability_flags()
    # Send flags fail closed when absent; cowork is the deliberate exception
    # (capture gate, ships on-by-default per its spec).
    assert flags == {
        "email_send": False, "imessage_send": False, "whatsapp_send": False,
        "slack_send": False,
        "cowork_window": True, "cowork_clipboard": True,
        "cowork_files": True, "notetaker": False, "calendar_write": False,
        "computer_use": False,
    }


def test_capability_flags_missing_file_is_deny(repo_root):
    # No config written at all → deny, never raises. Even default-yes flags
    # fail closed here: no readable config means no grants of any kind.
    assert C.capability_flags() == {
        "email_send": False, "imessage_send": False, "whatsapp_send": False,
        "slack_send": False,
        "cowork_window": False, "cowork_clipboard": False,
        "cowork_files": False, "notetaker": False, "calendar_write": False,
        "computer_use": False,
    }


def test_capability_flags_reads_grants(repo_root):
    _write_config(
        repo_root,
        """
capabilities:
  email_send: true
  imessage_send: false
pais:
  - name: root
    pid: 1
    description: km
""",
    )
    flags = C.capability_flags()
    assert flags["email_send"] is True
    assert flags["imessage_send"] is False


def test_project_capabilities_writes_and_clears_freeze(repo_root, tmp_path, monkeypatch):
    monkeypatch.setattr(PA, "PAI_ROOT", tmp_path, raising=True)
    email_freeze = PA.sys_drivers("email") / "outbound.freeze"
    imsg_freeze = PA.sys_drivers("imessage") / "outbound.freeze"

    # Denied (the default): both freeze files written.
    _write_config(
        repo_root,
        """
capabilities:
  email_send: false
  imessage_send: false
pais:
  - name: root
    pid: 1
    description: km
""",
    )
    C.project_capabilities()
    assert email_freeze.exists()
    assert imsg_freeze.exists()
    assert "email_send" in email_freeze.read_text()

    # Grant email → its freeze is removed; imessage stays frozen.
    _write_config(
        repo_root,
        """
capabilities:
  email_send: true
  imessage_send: false
pais:
  - name: root
    pid: 1
    description: km
""",
    )
    C.project_capabilities()
    assert not email_freeze.exists()
    assert imsg_freeze.exists()


def test_project_capabilities_idempotent_clear(repo_root, tmp_path, monkeypatch):
    # Granting when no freeze exists must not raise (unlink of a missing file).
    monkeypatch.setattr(PA, "PAI_ROOT", tmp_path, raising=True)
    _write_config(
        repo_root,
        """
capabilities:
  email_send: true
  imessage_send: true
pais:
  - name: root
    pid: 1
    description: km
""",
    )
    C.project_capabilities()
    C.project_capabilities()  # second pass, still clear
    assert not (PA.sys_drivers("email") / "outbound.freeze").exists()
    assert not (PA.sys_drivers("imessage") / "outbound.freeze").exists()


def test_default_config_yaml_seeds_capabilities():
    denied = yaml.safe_load(default_config_yaml())
    assert denied["capabilities"] == {
        "email_send": False, "imessage_send": False, "calendar_write": False,
    }

    granted = yaml.safe_load(
        default_config_yaml(email_send=True, imessage_send=True)
    )
    # calendar_write is not wired to an install-time consent question — it seeds
    # off regardless and the owner flips it in the console.
    assert granted["capabilities"] == {
        "email_send": True, "imessage_send": True, "calendar_write": False,
    }


# ----- capabilities: tri-state modes (no / ask / yes) -----


def test_capability_modes_parses_all_three(repo_root):
    _write_config(
        repo_root,
        """
capabilities:
  email_send: ask
  imessage_send: yes
pais:
  - name: root
    pid: 1
    description: km
""",
    )
    modes = C.capability_modes()
    assert modes == {
        "email_send": "ask", "imessage_send": "yes", "whatsapp_send": "no",
        "slack_send": "no",
        "cowork_window": "yes", "cowork_clipboard": "yes",
        "cowork_files": "yes", "notetaker": "no", "calendar_write": "no",
        "computer_use": "no",
    }


def test_capability_modes_legacy_bools_map(repo_root):
    # true→yes, false→no so existing configs keep their meaning.
    _write_config(
        repo_root,
        """
capabilities:
  email_send: true
  imessage_send: false
pais:
  - name: root
    pid: 1
    description: km
""",
    )
    assert C.capability_modes() == {
        "email_send": "yes", "imessage_send": "no", "whatsapp_send": "no",
        "slack_send": "no",
        "cowork_window": "yes", "cowork_clipboard": "yes",
        "cowork_files": "yes", "notetaker": "no", "calendar_write": "no",
        "computer_use": "no",
    }


def test_capability_modes_unknown_value_fails_closed(repo_root):
    _write_config(
        repo_root,
        """
capabilities:
  email_send: maybe
  imessage_send: 7
pais:
  - name: root
    pid: 1
    description: km
""",
    )
    assert C.capability_modes() == {
        "email_send": "no", "imessage_send": "no", "whatsapp_send": "no",
        "slack_send": "no",
        "cowork_window": "yes", "cowork_clipboard": "yes",
        "cowork_files": "yes", "notetaker": "no", "calendar_write": "no",
        "computer_use": "no",
    }


def test_capability_flags_ask_is_not_direct_send(repo_root):
    # ask must NOT clear the freeze — the PAI can't send directly.
    _write_config(
        repo_root,
        """
capabilities:
  email_send: ask
  imessage_send: no
pais:
  - name: root
    pid: 1
    description: km
""",
    )
    flags = C.capability_flags()
    assert flags == {
        "email_send": False, "imessage_send": False, "whatsapp_send": False,
        "slack_send": False,
        "cowork_window": True, "cowork_clipboard": True,
        "cowork_files": True, "notetaker": False, "calendar_write": False,
        "computer_use": False,
    }


def test_project_capabilities_ask_keeps_freeze(repo_root, tmp_path, monkeypatch):
    monkeypatch.setattr(PA, "PAI_ROOT", tmp_path, raising=True)
    email_freeze = PA.sys_drivers("email") / "outbound.freeze"
    _write_config(
        repo_root,
        """
capabilities:
  email_send: ask
  imessage_send: yes
pais:
  - name: root
    pid: 1
    description: km
""",
    )
    C.project_capabilities()
    # ask → frozen (no direct PAI send), reason records the mode.
    assert email_freeze.exists()
    assert "email_send=ask" in email_freeze.read_text()
    # yes → cleared.
    assert not (PA.sys_drivers("imessage") / "outbound.freeze").exists()


def test_project_capabilities_whatsapp_freeze(repo_root, tmp_path, monkeypatch):
    # whatsapp_send projects onto sys/drivers/whatsapp/outbound.freeze exactly
    # like email/imessage: no/ask keep the freeze (DENY), yes clears it. Absent
    # from config → frozen (fail-closed).
    monkeypatch.setattr(PA, "PAI_ROOT", tmp_path, raising=True)
    wa_freeze = PA.sys_drivers("whatsapp") / "outbound.freeze"

    # Absent → frozen by default.
    _write_config(
        repo_root,
        """
pais:
  - name: root
    pid: 1
    description: km
""",
    )
    C.project_capabilities()
    assert wa_freeze.exists()
    assert "whatsapp_send=no" in wa_freeze.read_text()

    # ask → still frozen (no direct PAI send), reason records the mode.
    _write_config(
        repo_root,
        """
capabilities:
  whatsapp_send: ask
pais:
  - name: root
    pid: 1
    description: km
""",
    )
    C.project_capabilities()
    assert wa_freeze.exists()
    assert "whatsapp_send=ask" in wa_freeze.read_text()

    # yes → freeze cleared (direct sends enabled).
    _write_config(
        repo_root,
        """
capabilities:
  whatsapp_send: yes
pais:
  - name: root
    pid: 1
    description: km
""",
    )
    C.project_capabilities()
    assert not wa_freeze.exists()


# ----- capabilities: capture gates (cowork facets / notetaker) -----

_COWORK_FACETS = ("cowork_window", "cowork_clipboard", "cowork_files")


def test_cowork_defaults_yes_when_key_absent(repo_root):
    # capabilities block exists but carries no capture key: the cowork facets
    # are on-by-default (their specs' deliberate exception), notetaker fails
    # closed.
    _write_config(
        repo_root,
        """
capabilities:
  email_send: no
pais:
  - name: root
    pid: 1
    description: km
""",
    )
    modes = C.capability_modes()
    for facet in _COWORK_FACETS:
        assert modes[facet] == "yes"
    assert modes["notetaker"] == "no"


def test_capture_flag_explicit_values_respected(repo_root):
    _write_config(
        repo_root,
        """
capabilities:
  cowork_window: no
  cowork_clipboard: no
  cowork_files: yes
  notetaker: yes
pais:
  - name: root
    pid: 1
    description: km
""",
    )
    modes = C.capability_modes()
    assert modes["cowork_window"] == "no"
    assert modes["cowork_clipboard"] == "no"
    assert modes["cowork_files"] == "yes"
    assert modes["notetaker"] == "yes"


def test_legacy_cowork_key_seeds_all_facets(repo_root):
    # A pre-split config saying `cowork: no` must keep meaning "all capture
    # off" — the default-yes facets may not silently resurrect capture. A
    # facet key present alongside the legacy key wins.
    _write_config(
        repo_root,
        """
capabilities:
  cowork: no
  cowork_files: yes
pais:
  - name: root
    pid: 1
    description: km
""",
    )
    modes = C.capability_modes()
    assert modes["cowork_window"] == "no"
    assert modes["cowork_clipboard"] == "no"
    assert modes["cowork_files"] == "yes"


def test_capture_flag_ask_clamps_to_no(repo_root):
    # Capture flags are two-state; "ask" is not a capture mode and must fail
    # closed rather than half-grant.
    _write_config(
        repo_root,
        """
capabilities:
  cowork_window: ask
  cowork_clipboard: ask
  cowork_files: ask
  notetaker: ask
pais:
  - name: root
    pid: 1
    description: km
""",
    )
    modes = C.capability_modes()
    for facet in _COWORK_FACETS:
        assert modes[facet] == "no"
    assert modes["notetaker"] == "no"


def test_set_capability_mode_rejects_ask_for_capture_flags(repo_root):
    _write_config(
        repo_root,
        """
capabilities: {}
pais:
  - name: root
    pid: 1
    description: km
""",
    )
    with pytest.raises(ValueError):
        C.set_capability_mode("cowork_window", "ask")
    with pytest.raises(ValueError):
        C.set_capability_mode("notetaker", "ask")
    assert C.set_capability_mode("cowork_window", "no") == "no"
    assert C.set_capability_mode("notetaker", "yes") == "yes"


def test_project_capabilities_capture_freeze(repo_root, tmp_path, monkeypatch):
    # Cowork facets absent (default yes) → no freezes; notetaker absent
    # (default no) → capture.freeze written. Flipping one facet off writes
    # only that facet's freeze.
    monkeypatch.setattr(PA, "PAI_ROOT", tmp_path, raising=True)
    window_freeze = PA.sys_drivers("cowork") / "window.freeze"
    clipboard_freeze = PA.sys_drivers("cowork") / "clipboard.freeze"
    files_freeze = PA.sys_drivers("cowork") / "files.freeze"
    notetaker_freeze = PA.sys_drivers("notetaker") / "capture.freeze"

    _write_config(
        repo_root,
        """
pais:
  - name: root
    pid: 1
    description: km
""",
    )
    C.project_capabilities()
    assert not window_freeze.exists()
    assert not clipboard_freeze.exists()
    assert not files_freeze.exists()
    assert notetaker_freeze.exists()
    assert "notetaker=no" in notetaker_freeze.read_text()

    _write_config(
        repo_root,
        """
capabilities:
  cowork_clipboard: no
  notetaker: yes
pais:
  - name: root
    pid: 1
    description: km
""",
    )
    C.project_capabilities()
    assert not window_freeze.exists()
    assert clipboard_freeze.exists()
    assert "cowork_clipboard=no" in clipboard_freeze.read_text()
    assert not files_freeze.exists()
    assert not notetaker_freeze.exists()


def test_project_capabilities_drops_legacy_capture_freeze(
    repo_root, tmp_path, monkeypatch
):
    # The pre-split single capture.freeze is dead; every projection pass
    # removes it so a stale file can't contradict the per-facet gates.
    monkeypatch.setattr(PA, "PAI_ROOT", tmp_path, raising=True)
    legacy = PA.sys_drivers("cowork") / "capture.freeze"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text("frozen by capabilities.cowork=no in etc/config.yaml\n")
    _write_config(
        repo_root,
        """
pais:
  - name: root
    pid: 1
    description: km
""",
    )
    C.project_capabilities()
    assert not legacy.exists()


# ----- set_capability_mode (sidebar toggle writer) -----


def test_set_capability_mode_writes_and_reads_back(repo_root):
    _write_config(
        repo_root,
        """
capabilities:
  email_send: no
  imessage_send: no
pais:
  - name: root
    pid: 1
    description: km
""",
    )
    assert C.set_capability_mode("email_send", "ask") == "ask"
    modes = C.capability_modes()
    assert modes["email_send"] == "ask"
    # The untouched channel keeps its value.
    assert modes["imessage_send"] == "no"


def test_set_capability_mode_preserves_siblings(repo_root):
    path = _write_config(
        repo_root,
        """
onboarding_pending: false
capabilities:
  email_send: no
  imessage_send: "yes"
pais:
  - name: root
    pid: 1
    description: km
  - name: pai
    pid: 2
    description: dflt
""",
    )
    C.set_capability_mode("email_send", "yes")
    data = yaml.safe_load(path.read_text())
    # Fleet + top-level flags survive the round-trip; only the one mode changed.
    assert data["onboarding_pending"] is False
    assert [e["name"] for e in data["pais"]] == ["root", "pai"]
    assert data["capabilities"] == {"email_send": "yes", "imessage_send": "yes"}


def test_set_capability_mode_creates_block_when_absent(repo_root):
    path = _write_config(
        repo_root,
        """
pais:
  - name: root
    pid: 1
    description: km
""",
    )
    C.set_capability_mode("imessage_send", "ask")
    data = yaml.safe_load(path.read_text())
    assert data["capabilities"] == {"imessage_send": "ask"}


def test_set_capability_mode_rejects_unknown_flag(repo_root):
    _write_config(
        repo_root,
        """
pais:
  - name: root
    pid: 1
    description: km
""",
    )
    with pytest.raises(ValueError):
        C.set_capability_mode("sms_send", "yes")


def test_set_capability_mode_rejects_invalid_mode(repo_root):
    # Unlike the tolerant read path, the writer fails loud on a bad mode rather
    # than coercing to off — a typo here is a caller bug, not a grant.
    path = _write_config(
        repo_root,
        """
capabilities:
  email_send: ask
pais:
  - name: root
    pid: 1
    description: km
""",
    )
    with pytest.raises(ValueError):
        C.set_capability_mode("email_send", "maybe")
    # The file is untouched by the rejected write.
    assert yaml.safe_load(path.read_text())["capabilities"] == {"email_send": "ask"}


# ----- set_pai_model (per-PAI provider/model mutation) -----


def test_set_pai_model_rewrites_only_target(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "capabilities:\n"
        "  email_send: ask\n"
        "pais:\n"
        "- name: root\n"
        "  provider: deepseek\n"
        "  model: deepseek-v4-pro\n"
        "- name: pai\n"
        "  provider: deepseek\n"
        "  model: deepseek-v4-pro\n"
        "  fallback: true\n"
    )
    out = C.set_pai_model("pai", "openrouter", "moonshotai/kimi-k2:free", path=cfg)
    assert out == {"name": "pai", "provider": "openrouter", "model": "moonshotai/kimi-k2:free"}
    data = yaml.safe_load(cfg.read_text())
    by_name = {e["name"]: e for e in data["pais"]}
    assert by_name["pai"]["provider"] == "openrouter"
    assert by_name["pai"]["model"] == "moonshotai/kimi-k2:free"
    assert by_name["pai"]["fallback"] is True          # untouched siblings keys
    assert by_name["root"]["provider"] == "deepseek"   # untouched sibling entry
    assert data["capabilities"] == {"email_send": "ask"}  # untouched other sections


def test_set_pai_model_unknown_provider(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("pais:\n- name: pai\n")
    with pytest.raises(ValueError, match="unknown provider"):
        C.set_pai_model("pai", "grok", "grok-5", path=cfg)


def test_set_pai_model_unknown_pai(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("pais:\n- name: pai\n")
    with pytest.raises(ValueError, match="unknown pai"):
        C.set_pai_model("ghost", "anthropic", "claude-opus-4-8", path=cfg)


def test_set_pai_model_rejects_empty_model(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("pais:\n- name: pai\n")
    with pytest.raises(ValueError, match="model"):
        C.set_pai_model("pai", "anthropic", "   ", path=cfg)


def test_set_pai_model_malformed_yaml_leaves_file_untouched(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("pais: [unclosed\n")
    before = cfg.read_text()
    with pytest.raises(Exception):
        C.set_pai_model("pai", "anthropic", "claude-opus-4-8", path=cfg)
    assert cfg.read_text() == before
