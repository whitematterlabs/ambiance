"""Subagent lifecycle: persistent spawn, two-channel messaging, parent-reaps.

Subagents differ from one-shot ephemerals in two ways:
  1. Their spawn spec carries `persistent: true`, so nudge.py does NOT
     auto-resolve them after the initial-prompt turn.
  2. Termination is explicit: the standard exit is `bin/subagent done --result`,
     which emits a final `subagent:response` pointing at the durable report
     *and* resolves the child's proc as completed in that same call.
     The legacy `reply --done` path remains compatible. The parent may also call
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
from boot import main as main_mod
from boot import nudge as nudge_mod
from boot import paths as paths_mod
from boot import processes as P


def _events(events_dir: Path) -> list[dict]:
    out = []
    for p in sorted(events_dir.iterdir()):
        with p.open() as f:
            out.append(yaml.safe_load(f) or {})
    return out


def _spawn_parent_child(parent_slug: str = "root", child_slug: str = "child-real") -> int:
    P.spawn_pai(pid=1, slug=parent_slug, description="parent")
    P.spawn_pai(
        pid=7,
        slug=child_slug,
        description="child",
        parent=1,
        extra={"persistent": True},
    )
    return 7


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


def test_spawn_prompt_preserves_dollar_budget_when_quoted(
    live_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    P.spawn_pai(pid=1, slug="root", description="parent")
    monkeypatch.setenv("PAI_PID", "1")
    prompt = "Correction: budget is explicitly USD $1,200 to $1,500 per month."

    rc = sub_bin.main(["spawn", "--slug", "housing", "--prompt", prompt])
    assert rc == 0

    [child_slug] = [s for s in P.list_procs() if s.startswith("housing-")]
    spec = P.read_spec(child_slug)
    assert "$1,200" in spec["description"]
    [kickoff] = _events(P.EVENTS_DIR)
    assert kickoff["text"] == prompt


@pytest.mark.parametrize(
    "prompt",
    [
        "Correction: budget is explicitly USD ,200 to ,500 per month.",
        "Find housing with budget about .5k/month near transit.",
    ],
)
def test_spawn_rejects_shell_mangled_budget_prompt(
    live_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    prompt: str,
) -> None:
    P.spawn_pai(pid=1, slug="root", description="parent")
    monkeypatch.setenv("PAI_PID", "1")

    rc = sub_bin.main(["spawn", "--slug", "housing", "--prompt", prompt])
    assert rc == 1

    assert not [s for s in P.list_procs() if s.startswith("housing-")]
    assert not list(P.EVENTS_DIR.iterdir())
    captured = capsys.readouterr()
    assert "shell-mangled" in captured.err
    assert "single quotes" in captured.err


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


def test_done_result_emits_pointer_and_resolves(
    live_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    P.spawn_pai(pid=1, slug="root", description="parent")
    monkeypatch.setenv("PAI_PID", "1")
    sub_bin.main(["spawn", "--slug", "scratch", "--prompt", "go"])
    [child_slug] = [s for s in P.list_procs() if s.startswith("scratch-")]
    child_pid = P.read_spec(child_slug)["pid"]

    for e in P.EVENTS_DIR.iterdir():
        e.unlink()

    parent_home = P.HOME_DIR / "root"
    result_dir = parent_home / "workspace" / child_slug
    result_dir.mkdir(parents=True)
    (result_dir / "result.md").write_text("final answer lives here\n")

    monkeypatch.setenv("PAI_PID", str(child_pid))
    monkeypatch.setenv("PAI_PARENT", "1")
    monkeypatch.setenv("PAI_SLUG", child_slug)
    monkeypatch.setenv("PAI_PARENT_HOME", str(parent_home))
    rc = sub_bin.main(["done", "--result", "result.md"])
    assert rc == 0

    assert child_slug not in P.list_procs()
    events = _events(P.EVENTS_DIR)
    assert len(events) == 2
    resp, resolved = events
    result_ref = f"workspace/{child_slug}/result.md"
    assert resp["kind"] == "subagent:response"
    assert resp["target_pid"] == 1
    assert resp["sender_pid"] == child_pid
    assert resp["text"] == f"done: {result_ref}"
    assert resp["result"] == result_ref
    assert resp.get("done") is True
    assert resolved["kind"] == "proc_resolved"
    assert resolved["slug"] == child_slug
    assert resolved["status"] == "completed"
    assert "parent" not in resolved


def test_reply_done_uses_live_slug_when_env_slug_is_stale(
    live_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    child_slug = "child-real"
    child_pid = _spawn_parent_child(child_slug=child_slug)

    monkeypatch.setenv("PAI_PID", str(child_pid))
    monkeypatch.setenv("PAI_PARENT", "1")
    monkeypatch.setenv("PAI_SLUG", "child-old")
    rc = sub_bin.main(["reply", "--done", "--content", "final answer"])
    assert rc == 0

    captured = capsys.readouterr()
    assert "PAI_SLUG='child-old'" in captured.err
    assert "child-real" in captured.err
    assert child_slug not in P.list_procs()
    resp, resolved = _events(P.EVENTS_DIR)
    assert resp["kind"] == "subagent:response"
    assert resolved["slug"] == child_slug


def test_done_uses_live_slug_when_env_slug_is_stale(
    live_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    child_slug = "child-real"
    child_pid = _spawn_parent_child(child_slug=child_slug)
    parent_home = P.HOME_DIR / "root"
    result_dir = parent_home / "workspace" / child_slug
    result_dir.mkdir(parents=True)
    (result_dir / "result.md").write_text("final answer lives here\n")

    monkeypatch.setenv("PAI_PID", str(child_pid))
    monkeypatch.setenv("PAI_PARENT", "1")
    monkeypatch.setenv("PAI_SLUG", "child-old")
    monkeypatch.setenv("PAI_PARENT_HOME", str(parent_home))
    rc = sub_bin.main(["done", "--result", "result.md"])
    assert rc == 0

    captured = capsys.readouterr()
    assert "PAI_SLUG='child-old'" in captured.err
    assert "child-real" in captured.err
    assert child_slug not in P.list_procs()
    resp, resolved = _events(P.EVENTS_DIR)
    result_ref = "workspace/child-real/result.md"
    assert resp["result"] == result_ref
    assert resp["text"] == f"done: {result_ref}"
    assert resolved["slug"] == child_slug


def test_plan_ready_uses_live_slug_when_env_slug_is_stale(
    live_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    child_pid = _spawn_parent_child(child_slug="child-real")

    monkeypatch.setenv("PAI_PID", str(child_pid))
    monkeypatch.setenv("PAI_PARENT", "1")
    monkeypatch.setenv("PAI_SLUG", "child-old")
    rc = sub_bin.main(["plan-ready", "--content", "ready"])
    assert rc == 0

    captured = capsys.readouterr()
    assert "PAI_SLUG='child-old'" in captured.err
    [event] = _events(P.EVENTS_DIR)
    assert event["kind"] == "subagent:plan_ready"
    assert event["slug"] == "child-real"


def test_done_result_prefers_pai_result_dir(
    live_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    child_slug = "child-real"
    child_pid = _spawn_parent_child(child_slug=child_slug)
    parent_home = P.HOME_DIR / "root"
    result_dir = tmp_path / "durable-workspace" / child_slug
    result_dir.mkdir(parents=True)
    (result_dir / "result.md").write_text("final answer lives here\n")

    monkeypatch.setenv("PAI_PID", str(child_pid))
    monkeypatch.setenv("PAI_PARENT", "1")
    monkeypatch.setenv("PAI_SLUG", child_slug)
    monkeypatch.setenv("PAI_PARENT_HOME", str(parent_home))
    monkeypatch.setenv("PAI_RESULT_DIR", str(result_dir))
    rc = sub_bin.main(["done", "--result", "result.md"])
    assert rc == 0

    [resp, _resolved] = _events(P.EVENTS_DIR)
    assert resp["result"] == "workspace/child-real/result.md"


def test_nudge_env_exposes_resolved_pai_result_dir(
    live_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    child_slug = "child-real"
    child_pid = _spawn_parent_child(child_slug=child_slug)
    parent_home = P.HOME_DIR / "root"
    parent_home.mkdir(parents=True)
    durable_workspace = tmp_path / "var" / "lib" / "instances" / "root" / "workspace"
    durable_workspace.mkdir(parents=True)
    (parent_home / "workspace").symlink_to(durable_workspace, target_is_directory=True)
    captured_env: dict[str, str] = {}

    def fake_home_for(slug: str) -> Path:
        return parent_home if slug == "root" else live_dir / slug

    async def fake_run_turn(*args, env: dict | None = None, history=None, **kwargs):
        captured_env.update(env or {})
        return "", list(history or [])

    monkeypatch.setattr(nudge_mod, "HOME_DIR", live_dir, raising=True)
    monkeypatch.setattr(nudge_mod, "PROC_DIR", P.PROC_DIR, raising=True)
    monkeypatch.setattr(nudge_mod.stitch, "home_for", fake_home_for)
    monkeypatch.setattr(nudge_mod.bootstrap, "build_system_prompt", lambda **kwargs: "system")
    monkeypatch.setattr(nudge_mod.bootstrap, "build_user_turn", lambda *args, **kwargs: "user")
    monkeypatch.setattr(nudge_mod.llm, "run_turn", fake_run_turn)

    asyncio.run(
        nudge_mod._nudge_body(
            reason="peer message",
            slug=None,
            context=None,
            pai_pid=child_pid,
            pai_slug=child_slug,
            from_=1,
            from_kind="pai",
        )
    )

    assert captured_env["PAI_PARENT_HOME"] == str(parent_home)
    assert captured_env["PAI_RESULT_DIR"] == str(
        (parent_home / "workspace" / child_slug).resolve(strict=False)
    )


def test_self_finished_subagent_with_trailing_text_does_not_double_exit(
    live_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A child that calls `bin/subagent done` *and* leaves trailing assistant
    text must not trigger the kernel's auto-finish fallback on top of its own
    exit. The child reaps its own /proc during the turn; the stale turn-start
    spec must not make the kernel emit a second subagent:response and then fail
    its redundant P.resolve with ProcessNotFound ("auto-finish failed")."""
    child_slug = "child-self-done"
    child_pid = _spawn_parent_child(child_slug=child_slug)
    parent_home = P.HOME_DIR / "root"
    parent_home.mkdir(parents=True)

    def fake_home_for(slug: str) -> Path:
        home = P.HOME_DIR / slug
        home.mkdir(parents=True, exist_ok=True)
        return home

    async def fake_run_turn(*args, history=None, **kwargs):
        # Simulate the child invoking `bin/subagent done --result result.md`
        # mid-turn: emit the real done response to the parent, then reap its
        # own proc — exactly what bin/subagent's _resolve_done does.
        result_dir = parent_home / "workspace" / child_slug
        result_dir.mkdir(parents=True, exist_ok=True)
        (result_dir / "result.md").write_text("real report\n")
        result_ref = f"workspace/{child_slug}/result.md"
        P.emit_event(
            {
                "source": "subagent",
                "kind": "subagent:response",
                "target_pid": 1,
                "sender_pid": child_pid,
                "text": f"done: {result_ref}",
                "done": True,
                "result": result_ref,
            }
        )
        P.resolve(child_slug, "completed", notify_parent=False)
        messages = list(history or [])
        messages.append(
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "All set — report saved."}],
            }
        )
        # Model leaves a trailing summary sentence after the done tool call.
        return "All set — report saved.", messages

    monkeypatch.setattr(nudge_mod, "HOME_DIR", P.HOME_DIR, raising=True)
    monkeypatch.setattr(nudge_mod, "PROC_DIR", P.PROC_DIR, raising=True)
    monkeypatch.setattr(nudge_mod.stitch, "home_for", fake_home_for)
    monkeypatch.setattr(nudge_mod.bootstrap, "build_system_prompt", lambda **kwargs: "system")
    monkeypatch.setattr(nudge_mod.bootstrap, "build_user_turn", lambda *args, **kwargs: "user")
    monkeypatch.setattr(nudge_mod.llm, "run_turn", fake_run_turn)

    asyncio.run(
        nudge_mod._nudge_body(
            reason="peer message",
            slug=None,
            context=None,
            pai_pid=child_pid,
            pai_slug=child_slug,
            from_=1,
            from_kind="pai",
        )
    )

    events = _events(P.EVENTS_DIR)
    responses = [e for e in events if e.get("kind") == "subagent:response"]
    # Exactly one done response — the child's own. No auto-fallback duplicate.
    assert len(responses) == 1, responses
    assert responses[0].get("auto_fallback") is not True
    assert responses[0]["text"] == f"done: workspace/{child_slug}/result.md"

    # The child's own result must not be clobbered by the fallback writer.
    handoff = parent_home / "workspace" / child_slug / "result.md"
    assert handoff.read_text() == "real report\n"

    # The kernel must not have tried (and failed) a redundant second exit.
    assert "auto-finish failed" not in capsys.readouterr().out


def test_plain_text_subagent_reply_auto_finishes(
    live_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    child_slug = "child-real"
    child_pid = _spawn_parent_child(child_slug=child_slug)
    parent_home = P.HOME_DIR / "root"
    parent_home.mkdir(parents=True)

    def fake_home_for(slug: str) -> Path:
        home = P.HOME_DIR / slug
        home.mkdir(parents=True, exist_ok=True)
        return home

    async def fake_run_turn(*args, history=None, **kwargs):
        messages = list(history or [])
        messages.append(
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "Final housing report"}],
            }
        )
        return "Final housing report", messages

    monkeypatch.setattr(nudge_mod, "HOME_DIR", P.HOME_DIR, raising=True)
    monkeypatch.setattr(nudge_mod, "PROC_DIR", P.PROC_DIR, raising=True)
    monkeypatch.setattr(nudge_mod.stitch, "home_for", fake_home_for)
    monkeypatch.setattr(nudge_mod.bootstrap, "build_system_prompt", lambda **kwargs: "system")
    monkeypatch.setattr(nudge_mod.bootstrap, "build_user_turn", lambda *args, **kwargs: "user")
    monkeypatch.setattr(nudge_mod.llm, "run_turn", fake_run_turn)

    asyncio.run(
        nudge_mod._nudge_body(
            reason="peer message",
            slug=None,
            context=None,
            pai_pid=child_pid,
            pai_slug=child_slug,
            from_=1,
            from_kind="pai",
        )
    )

    assert child_slug not in P.list_procs()
    handoff = parent_home / "workspace" / child_slug / "result.md"
    assert handoff.read_text() == "Final housing report\n"

    events = _events(P.EVENTS_DIR)
    responses = [e for e in events if e.get("kind") == "subagent:response"]
    assert len(responses) == 1
    resp = responses[0]
    result_ref = f"workspace/{child_slug}/result.md"
    assert resp["target_pid"] == 1
    assert resp["sender_pid"] == child_pid
    assert resp["text"] == (
        "auto-fallback: child ended without calling "
        f"`bin/subagent done`; saved plain reply to {result_ref}"
    )
    assert resp["result"] == result_ref
    assert resp["done"] is True
    assert resp["auto_fallback"] is True

    resolved = [e for e in events if e.get("kind") == "proc_resolved"]
    assert len(resolved) == 1
    assert resolved[0]["slug"] == child_slug
    assert "parent" not in resolved[0]


@pytest.mark.parametrize("use_resolved_target", [False, True])
def test_done_result_accepts_absolute_parent_workspace_symlink_and_target_paths(
    live_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    use_resolved_target: bool,
) -> None:
    child_slug = "child-real"
    child_pid = _spawn_parent_child(child_slug=child_slug)
    parent_home = P.HOME_DIR / "root"
    parent_home.mkdir(parents=True)
    durable_workspace = tmp_path / "var" / "lib" / "instances" / "root" / "workspace"
    durable_workspace.mkdir(parents=True)
    (parent_home / "workspace").symlink_to(durable_workspace, target_is_directory=True)

    base = durable_workspace if use_resolved_target else parent_home / "workspace"
    result_path = base / child_slug / "result.md"
    result_path.parent.mkdir(parents=True)
    result_path.write_text("final answer lives here\n")

    monkeypatch.setenv("PAI_PID", str(child_pid))
    monkeypatch.setenv("PAI_PARENT", "1")
    monkeypatch.setenv("PAI_SLUG", child_slug)
    monkeypatch.setenv("PAI_PARENT_HOME", str(parent_home))
    rc = sub_bin.main(["done", "--result", str(result_path)])
    assert rc == 0

    [resp, _resolved] = _events(P.EVENTS_DIR)
    assert resp["result"] == "workspace/child-real/result.md"


def test_done_result_requires_existing_parent_workspace_file(
    live_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    P.spawn_pai(pid=1, slug="root", description="parent")
    monkeypatch.setenv("PAI_PID", "1")
    sub_bin.main(["spawn", "--slug", "scratch", "--prompt", "go"])
    [child_slug] = [s for s in P.list_procs() if s.startswith("scratch-")]
    child_pid = P.read_spec(child_slug)["pid"]

    for e in P.EVENTS_DIR.iterdir():
        e.unlink()

    parent_home = P.HOME_DIR / "root"
    monkeypatch.setenv("PAI_PID", str(child_pid))
    monkeypatch.setenv("PAI_PARENT", "1")
    monkeypatch.setenv("PAI_SLUG", child_slug)
    monkeypatch.setenv("PAI_PARENT_HOME", str(parent_home))
    rc = sub_bin.main(["done", "--result", "result.md"])
    assert rc == 1
    assert child_slug in P.list_procs()
    assert not list(P.EVENTS_DIR.iterdir())


def test_subagent_response_event_routes_result_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[dict] = []

    def fake_dispatch(to: int, *args, from_: int | None = None, **kwargs):
        captured.append({"to": to, "args": args, "from": from_, "kwargs": kwargs})
        return None

    monkeypatch.setattr(main_mod, "_dispatch_nudge", fake_dispatch)
    event_path = tmp_path / "event.yaml"
    event_path.write_text(
        yaml.safe_dump(
            {
                "source": "subagent",
                "kind": "subagent:response",
                "target_pid": 1,
                "sender_pid": 7,
                "text": "done: workspace/scout/result.md",
                "done": True,
                "result": "workspace/scout/result.md",
                "auto_fallback": True,
            }
        )
    )

    asyncio.run(main_mod._handle_event_file(event_path, []))

    assert captured == [
        {
            "to": 1,
            "args": ("subagent response",),
            "from": 7,
            "kwargs": {
                "from_kind": "subagent",
                "context": {
                    "text": "done: workspace/scout/result.md",
                    "done": True,
                    "result": "workspace/scout/result.md",
                    "auto_fallback": True,
                },
            },
        }
    ]


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


def test_owner_interrupt_cascades_to_ad_hoc_subagents(
    live_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Interrupting a PAI stops the ad-hoc subagents it spawned.

    Regression for 2026-07-03: a `browse` subagent kept hammering LinkedIn
    after the owner tried to stop PAI, because `interrupt` cancelled only the
    parent's own nudge task and never reached the child proc. The owner's stop
    button must reach delegated work — recursively — while leaving long-lived
    `persub` singletons alone.
    """
    import contextlib
    from collections import defaultdict

    # Isolate the module-global nudge registry from other tests.
    monkeypatch.setattr(main_mod, "_active_nudges", defaultdict(set))

    P.spawn_pai(pid=1, slug="root", description="parent")
    P.spawn_pai(pid=7, slug="scout-x", description="child", parent=1,
                extra={"persistent": True})
    P.spawn_pai(pid=9, slug="scout-x.deep", description="grandchild", parent=7,
                extra={"persistent": True})
    # A persub sibling that is long-lived by design and must survive.
    P.spawn_pai(pid=5, slug="root.computer-use", description="persub", parent=1,
                extra={"persistent": True, "persub": True})

    for e in P.EVENTS_DIR.iterdir():
        e.unlink()
    event_path = tmp_path / "interrupt.yaml"
    event_path.write_text(yaml.safe_dump({"kind": "interrupt", "pai": 1}))

    async def _drive():
        async def _forever():
            await asyncio.sleep(3600)

        parent_task = asyncio.ensure_future(_forever())
        child_task = asyncio.ensure_future(_forever())
        grand_task = asyncio.ensure_future(_forever())
        tasks = (parent_task, child_task, grand_task)
        main_mod._active_nudges[1].add(parent_task)
        main_mod._active_nudges[7].add(child_task)
        main_mod._active_nudges[9].add(grand_task)

        try:
            await main_mod._handle_event_file(event_path, [])
            # Let the handler's own cancellations settle. A few loop turns is
            # enough for a cancelled sleep() to finish; a task the cascade
            # missed simply stays pending, so its `.cancelled()` reads False and
            # the assertion fails fast instead of hanging on sleep(3600).
            for _ in range(5):
                await asyncio.sleep(0)
            return tuple(t.cancelled() for t in tasks)
        finally:
            for t in tasks:
                if not t.done():
                    t.cancel()
            for t in tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await t

    parent_cancelled, child_cancelled, grand_cancelled = asyncio.run(_drive())

    # Parent's own turn and both descendants' turns are cancelled.
    assert parent_cancelled
    assert child_cancelled
    assert grand_cancelled

    # Ad-hoc child + grandchild are reaped so they can't start another turn.
    procs = P.list_procs()
    assert "scout-x" not in procs
    assert "scout-x.deep" not in procs

    # The persub singleton is untouched.
    assert "root.computer-use" in procs
    assert P.read_status("root.computer-use") == "running"


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


def test_spawn_refuses_slug_that_implies_omitted_package(
    live_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    root = tmp_path / "pairoot"
    subagents = root / "usr" / "lib" / "subagents"
    pkg = subagents / "browse"
    pkg.mkdir(parents=True)
    (pkg / "package.yaml").write_text(
        "name: browse\n"
        "kind: subagent\n"
        "description: CDP browser operator\n"
    )

    monkeypatch.setattr(C, "SUBAGENTS_DIR", subagents, raising=True)

    P.spawn_pai(pid=1, slug="root", description="parent")
    monkeypatch.setenv("PAI_PID", "1")

    rc = sub_bin.main(
        [
            "spawn",
            "--slug",
            "sf-apt-search-browse",
            "--prompt",
            "open Chrome and search Craigslist",
        ]
    )

    assert rc == 1
    assert not [s for s in P.list_procs() if s.startswith("sf-apt-search-browse")]
    assert "--package browse" in capsys.readouterr().err


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
    monkeypatch.delenv("PAI_SLUG", raising=False)
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
