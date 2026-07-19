"""`subagent spawn --suicide-allowed no` — the child cannot end itself.

The flag lands in the child spec as `suicide_allowed: False` (absent means
the default: may end itself). Enforced at the self-finish choke point
(`subagent done`) and by the kernel's plain-reply
fallback, which relays instead of auto-finishing. Only the parent's
`subagent kill` reaps such a child.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import yaml

from bin import subagent as sub_bin
from boot import nudge as nudge_mod
from boot import processes as P


def _events(events_dir: Path) -> list[dict]:
    out = []
    for f in sorted(events_dir.iterdir()):
        data = yaml.safe_load(f.read_text())
        if isinstance(data, dict):
            out.append(data)
    return out


def test_spawn_flag_lands_in_spec_and_notice(
    live_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    P.spawn_pai(pid=1, slug="root", description="parent")
    monkeypatch.setenv("PAI_PID", "1")

    rc = sub_bin.main([
        "spawn", "--slug", "worker", "--prompt", "keep helping",
        "--suicide-allowed", "no",
    ])
    assert rc == 0

    [child_slug] = [s for s in P.list_procs() if s.startswith("worker-")]
    spec = P.read_spec(child_slug)
    assert spec["suicide_allowed"] is False

    out = capsys.readouterr().out
    assert "cannot end itself" in out
    assert "subagent kill" in out


def test_default_spawn_omits_flag_from_spec(
    live_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    P.spawn_pai(pid=1, slug="root", description="parent")
    monkeypatch.setenv("PAI_PID", "1")

    rc = sub_bin.main(["spawn", "--slug", "oneshot", "--prompt", "one task"])
    assert rc == 0

    [child_slug] = [s for s in P.list_procs() if s.startswith("oneshot-")]
    assert "suicide_allowed" not in P.read_spec(child_slug)


def _spawn_no_suicide_child(child_slug: str = "worker-child") -> int:
    P.spawn_pai(pid=1, slug="root", description="parent")
    P.spawn_pai(
        pid=7,
        slug=child_slug,
        description="child",
        parent=1,
        extra={"persistent": True, "suicide_allowed": False},
    )
    return 7


def test_self_finish_denied(
    live_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    child_slug = "worker-child"
    child_pid = _spawn_no_suicide_child(child_slug)
    monkeypatch.setenv("PAI_PID", str(child_pid))
    monkeypatch.setenv("PAI_PARENT", "1")
    monkeypatch.setenv("PAI_SLUG", child_slug)

    rc = sub_bin.main(["done", "--result", "result.md"])
    assert rc == 1
    assert "--suicide-allowed no" in capsys.readouterr().err
    # Still alive, nothing reached the parent.
    assert child_slug in P.list_procs()
    assert not [e for e in _events(P.EVENTS_DIR) if e.get("kind") == "subagent:response"]


def test_kernel_plain_reply_relays_instead_of_auto_finishing(
    live_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A no-suicide child ending its turn with plain text must NOT be reaped
    by the auto-finish fallback — the text is relayed as an intermediate
    subagent:response and the child stays alive."""
    child_slug = "worker-child"
    child_pid = _spawn_no_suicide_child(child_slug)
    parent_home = P.HOME_DIR / "root"
    parent_home.mkdir(parents=True)

    def fake_home_for(slug: str) -> Path:
        home = P.HOME_DIR / slug
        home.mkdir(parents=True, exist_ok=True)
        return home

    async def fake_run_turn(*args, history=None, **kwargs):
        messages = list(history or [])
        messages.append({
            "role": "assistant",
            "content": [{"type": "text", "text": "status: found 3 leads"}],
        })
        return "status: found 3 leads", messages

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

    # Alive, nothing written to the parent's workspace, no resolve.
    assert child_slug in P.list_procs()
    assert not (parent_home / "workspace" / child_slug / "result.md").exists()
    events = _events(P.EVENTS_DIR)
    assert not [e for e in events if e.get("kind") == "proc_resolved"]

    [resp] = [e for e in events if e.get("kind") == "subagent:response"]
    assert resp["target_pid"] == 1
    assert resp["sender_pid"] == child_pid
    assert resp["text"] == "status: found 3 leads"
    assert "done" not in resp
    assert "auto_fallback" not in resp
