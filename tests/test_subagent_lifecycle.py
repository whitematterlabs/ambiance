"""Subagent lifecycle: persistent spawn, two-channel messaging, parent-reaps.

Subagents differ from one-shot ephemerals in two ways:
  1. Their spawn spec carries `persistent: true`, so nudge.py does NOT
     auto-resolve them after the initial-prompt turn.
  2. Termination is explicit: the standard exit is `bin/subagent reply --done`,
     which emits the final `subagent:response` *and* resolves the child's
     proc as completed in that same call. The parent may also call
     `bin/subagent kill --slug X` to abort a child early. Self-kill is
     not allowed — kill is parent-only.

Two messaging kinds are tested:
  - parent→child rides generic `pai_message` (same as any peer IPC). The
    spawn kickoff is just the parent's first such IPC.
  - child→parent uses `subagent:response` (via `bin/subagent reply`), so
    the parent can recognize "this is from one of my own subagents" at a
    glance.

send-message ACK semantics (delivery verification) are exercised here
too: the kernel writes a per-msg ack/dropped file under /run/pai/acks/.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
import yaml

from bin import send_message as send_msg_bin
from bin import subagent as sub_bin
from boot import config as C
from boot import nudge as nudge_mod
from boot import paths as paths_mod
from boot import processes as P


def _events(events_dir: Path) -> list[dict]:
    out = []
    for p in sorted(events_dir.iterdir()):
        with p.open() as f:
            out.append(yaml.safe_load(f) or {})
    return out


@pytest.fixture
def acks_dir(live_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    acks = live_dir / "acks"
    acks.mkdir(parents=True)
    monkeypatch.setattr(P, "ACKS_DIR", acks, raising=True)
    monkeypatch.setattr(paths_mod, "ACKS_DIR", acks, raising=True)
    # send_message.py imported ACKS_DIR at import-time; rebind too.
    monkeypatch.setattr(send_msg_bin, "ACKS_DIR", acks, raising=True)
    return acks


def test_spawn_marks_subagent_persistent(live_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Parent PAI is pid 1. Subagent must be spawned from a PAI turn.
    P.spawn_pai(pid=1, slug="root", description="parent")
    monkeypatch.setenv("PAI_PID", "1")

    rc = sub_bin.main(["spawn", "--slug", "scratch", "--prompt", "do a thing"])
    assert rc == 0

    # Child proc exists with persistent + parent set.
    [child_slug] = [s for s in P.list_procs() if s.startswith("scratch-")]
    spec = P.read_spec(child_slug)
    assert spec["kind"] == "pai"
    assert spec["parent"] == 1
    assert spec["persistent"] is True
    assert isinstance(spec["pid"], int) and spec["pid"] != 1

    # Kickoff is just a generic pai_message — no special kind.
    [kickoff] = _events(P.EVENTS_DIR)
    assert kickoff["kind"] == "pai_message"
    assert kickoff["target_pid"] == spec["pid"]
    assert kickoff["sender_pid"] == 1
    assert kickoff["text"] == "do a thing"


def test_reply_done_emits_response_and_resolves(
    live_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    P.spawn_pai(pid=1, slug="root", description="parent")
    monkeypatch.setenv("PAI_PID", "1")
    sub_bin.main(["spawn", "--slug", "scratch", "--prompt", "go"])
    [child_slug] = [s for s in P.list_procs() if s.startswith("scratch-")]
    child_pid = P.read_spec(child_slug)["pid"]

    # Drop the kickoff event.
    for e in P.EVENTS_DIR.iterdir():
        e.unlink()

    # Child invokes `reply --done`.
    monkeypatch.setenv("PAI_PID", str(child_pid))
    monkeypatch.setenv("PAI_PARENT", "1")
    monkeypatch.setenv("PAI_SLUG", child_slug)
    rc = sub_bin.main(["reply", "--done", "--content", "final answer"])
    assert rc == 0

    # The proc is now reaped (ephemeral subagent + completed status).
    assert child_slug not in P.list_procs()

    # Two events emitted in order: subagent:response then proc_resolved.
    events = _events(P.EVENTS_DIR)
    assert len(events) == 2
    resp, resolved = events
    assert resp["kind"] == "subagent:response"
    assert resp["target_pid"] == 1
    assert resp["sender_pid"] == child_pid
    assert resp["text"] == "final answer"
    assert resp.get("done") is True
    assert resolved["kind"] == "proc_resolved"
    assert resolved["slug"] == child_slug
    assert resolved["status"] == "completed"
    # The `subagent:response` above is the parent's notification, so the
    # proc_resolved event resolves quietly: no `parent` pid means the kernel
    # won't fire a redundant "proc completed" nudge right behind the response.
    assert "parent" not in resolved


def test_persub_reply_done_is_rejected_without_event(
    live_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    P.spawn_pai(pid=1, slug="root", description="parent")
    P.spawn_pai(
        pid=5,
        slug="root.computer-use",
        description="macOS UI operator",
        parent=1,
        extra={"persistent": True, "persub": True},
    )

    monkeypatch.setenv("PAI_PID", "5")
    monkeypatch.setenv("PAI_PARENT", "1")
    monkeypatch.setenv("PAI_SLUG", "root.computer-use")
    rc = sub_bin.main(["reply", "--done", "--content", "final answer"])

    assert rc == 1
    assert P.read_status("root.computer-use") == "running"
    assert not list(P.EVENTS_DIR.iterdir())


def test_spawn_package_resolves_bundle_prompt(
    live_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "pairoot"
    subagents = root / "usr" / "lib" / "subagents"
    pkg = subagents / "computer-use"
    pkg.mkdir(parents=True)
    (pkg / "package.yaml").write_text(
        "name: computer-use\n"
        "kind: subagent\n"
        "version: 0.2.0\n"
        "description: macOS UI operator\n"
        "prompt: prompt.md\n"
    )
    (pkg / "prompt.md").write_text("drive local apps\n")

    monkeypatch.setattr(paths_mod, "PAI_ROOT", root, raising=True)
    monkeypatch.setattr(P, "PAI_ROOT", root, raising=True)
    monkeypatch.setattr(C, "SUBAGENTS_DIR", subagents, raising=True)

    P.spawn_pai(pid=1, slug="root", description="parent")
    monkeypatch.setenv("PAI_PID", "1")
    rc = sub_bin.main(
        [
            "spawn",
            "--slug",
            "computer-use",
            "--package",
            "computer-use",
            "--prompt",
            "go",
        ]
    )

    assert rc == 0
    [child_slug] = [s for s in P.list_procs() if s.startswith("computer-use-")]
    spec = P.read_spec(child_slug)
    assert spec["package"] == "computer-use"
    assert spec["prompt"] == str(pkg / "prompt.md")


def test_reply_without_done_does_not_resolve(
    live_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    P.spawn_pai(pid=1, slug="root", description="parent")
    monkeypatch.setenv("PAI_PID", "1")
    sub_bin.main(["spawn", "--slug", "scratch", "--prompt", "go"])
    [child_slug] = [s for s in P.list_procs() if s.startswith("scratch-")]
    child_pid = P.read_spec(child_slug)["pid"]

    for e in P.EVENTS_DIR.iterdir():
        e.unlink()

    monkeypatch.setenv("PAI_PID", str(child_pid))
    monkeypatch.setenv("PAI_PARENT", "1")
    monkeypatch.setenv("PAI_SLUG", child_slug)
    assert sub_bin.main(["reply", "--content", "still working"]) == 0
    assert P.read_status(child_slug) == "running"
    [resp] = _events(P.EVENTS_DIR)
    assert resp["kind"] == "subagent:response"
    assert "done" not in resp


def test_kill_rejects_self_kill(live_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The subagent itself can no longer use `kill` to self-terminate.
    P.spawn_pai(pid=1, slug="root", description="parent")
    monkeypatch.setenv("PAI_PID", "1")
    sub_bin.main(["spawn", "--slug", "scratch", "--prompt", "go"])
    [child_slug] = [s for s in P.list_procs() if s.startswith("scratch-")]
    child_pid = P.read_spec(child_slug)["pid"]

    monkeypatch.setenv("PAI_PID", str(child_pid))
    rc = sub_bin.main(["kill", "--slug", child_slug])
    assert rc == 1
    assert P.read_status(child_slug) == "running"


def test_kill_by_parent_works(live_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    P.spawn_pai(pid=1, slug="root", description="parent")
    monkeypatch.setenv("PAI_PID", "1")
    sub_bin.main(["spawn", "--slug", "scratch", "--prompt", "go"])
    [child_slug] = [s for s in P.list_procs() if s.startswith("scratch-")]

    # Parent (pid=1) aborts the child.
    monkeypatch.setenv("PAI_PID", "1")
    rc = sub_bin.main(["kill", "--slug", child_slug])
    assert rc == 0
    assert child_slug not in P.list_procs()


def test_kill_refuses_non_parent(live_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # pid 1 spawns a subagent; pid 99 (some other PAI) tries to kill it.
    P.spawn_pai(pid=1, slug="root", description="parent")
    monkeypatch.setenv("PAI_PID", "1")
    sub_bin.main(["spawn", "--slug", "scratch", "--prompt", "go"])
    [child_slug] = [s for s in P.list_procs() if s.startswith("scratch-")]

    monkeypatch.setenv("PAI_PID", "99")
    rc = sub_bin.main(["kill", "--slug", child_slug])
    assert rc == 1
    assert P.read_status(child_slug) == "running"


def test_send_message_acks_to_live_pid(
    live_dir: Path, acks_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Spawn sender (pid 1) and target (pid 2).
    P.spawn_pai(pid=1, slug="sender", description="s")
    P.spawn_pai(pid=2, slug="target", description="t")
    monkeypatch.setenv("PAI_PID", "1")

    # Stand in for the kernel: when send-message emits the event,
    # synthesize an ack file just like nudge.nudge() would.
    real_emit = P.emit_event

    def emit_and_ack(payload: dict):
        path = real_emit(payload)
        if payload.get("kind") == "pai_message" and payload.get("msg_id"):
            asyncio.run(
                nudge_mod.nudge(
                    reason="peer message",
                    to=int(payload["target_pid"]),
                    from_=int(payload["sender_pid"]),
                    context={"text": payload.get("text", "")},
                    msg_id=payload["msg_id"],
                    _exempt=True,
                )
            ) if False else None
            # Direct ack-only path: avoid actually running a full nudge.
            try:
                slug = P.find_pai_slug(int(payload["target_pid"]))
                P.emit_ack(payload["msg_id"], {
                    "kind": "pai_message:ack",
                    "msg_id": payload["msg_id"],
                    "target_pid": payload["target_pid"],
                    "slug": slug,
                })
            except P.ProcessNotFound:
                P.emit_ack(payload["msg_id"], {
                    "kind": "pai_message:dropped",
                    "msg_id": payload["msg_id"],
                    "target_pid": payload["target_pid"],
                    "reason": "no PAI with pid",
                })
        return path

    monkeypatch.setattr(P, "emit_event", emit_and_ack)
    monkeypatch.setattr(send_msg_bin.P, "emit_event", emit_and_ack)

    rc = send_msg_bin.main(["--to", "2", "--content", "hi", "--timeout", "1"])
    assert rc == 0


def test_send_message_to_stale_pid_drops(
    live_dir: Path, acks_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    P.spawn_pai(pid=1, slug="sender", description="s")
    monkeypatch.setenv("PAI_PID", "1")

    real_emit = P.emit_event

    def emit_and_ack(payload: dict):
        path = real_emit(payload)
        if payload.get("kind") == "pai_message" and payload.get("msg_id"):
            try:
                slug = P.find_pai_slug(int(payload["target_pid"]))
                P.emit_ack(payload["msg_id"], {
                    "kind": "pai_message:ack",
                    "msg_id": payload["msg_id"],
                    "target_pid": payload["target_pid"],
                    "slug": slug,
                })
            except P.ProcessNotFound:
                P.emit_ack(payload["msg_id"], {
                    "kind": "pai_message:dropped",
                    "msg_id": payload["msg_id"],
                    "target_pid": payload["target_pid"],
                    "reason": "no PAI with pid",
                })
        return path

    monkeypatch.setattr(P, "emit_event", emit_and_ack)
    monkeypatch.setattr(send_msg_bin.P, "emit_event", emit_and_ack)

    rc = send_msg_bin.main(["--to", "9999", "--content", "hi", "--timeout", "1"])
    assert rc == 1
    captured = capsys.readouterr()
    assert "9999" in captured.err


def test_reply_requires_parent_env(live_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Calling `subagent reply` without $PAI_PARENT (i.e., from a top-level PAI
    # that has no parent) is an error — only subagents can reply.
    monkeypatch.setenv("PAI_PID", "1")
    monkeypatch.delenv("PAI_PARENT", raising=False)
    rc = sub_bin.main(["reply", "--content", "no one to reply to"])
    assert rc == 1
    assert not list(P.EVENTS_DIR.iterdir())


def test_result_md_relocated_to_parent_on_reap(
    live_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A subagent's report (result.md) lives in its /proc/<slug>/, which is
    # reaped on resolve. The kernel relocates it into the parent's durable
    # workspace first, so the parent's `proc completed` nudge can read it.
    P.spawn_pai(pid=2, slug="pai", description="parent")
    monkeypatch.setenv("PAI_PID", "2")
    sub_bin.main(["spawn", "--slug", "scout", "--prompt", "find: x"])
    [child_slug] = [s for s in P.list_procs() if s.startswith("scout-")]

    # Subagent wrote its report into its own (ephemeral) proc dir.
    (P.PROC_DIR / child_slug / "result.md").write_text("answer: see foo.py:42\n")

    # Resolve → reap.
    P.resolve(child_slug, "completed")
    assert child_slug not in P.list_procs()
    assert not (P.PROC_DIR / child_slug).exists()

    # result.md survived in the parent's workspace.
    handoff = P.HOME_DIR / "pai" / "workspace" / child_slug / "result.md"
    assert handoff.is_file()
    assert "foo.py:42" in handoff.read_text()


def test_reap_without_result_md_is_clean(
    live_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A subagent that wrote no result.md reaps cleanly — no handoff dir,
    # no error.
    P.spawn_pai(pid=2, slug="pai", description="parent")
    monkeypatch.setenv("PAI_PID", "2")
    sub_bin.main(["spawn", "--slug", "scout", "--prompt", "find: x"])
    [child_slug] = [s for s in P.list_procs() if s.startswith("scout-")]

    P.resolve(child_slug, "completed")
    assert child_slug not in P.list_procs()
    handoff_dir = P.HOME_DIR / "pai" / "workspace" / child_slug
    assert not handoff_dir.exists()
