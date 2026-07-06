"""wake_on routing + per-PAI prompt wiring."""

from __future__ import annotations

from pathlib import Path

import pytest

from boot import bootstrap, main as M
from boot import paths
from boot import processes as P


def _spawn(
    slug: str,
    *,
    pid: int,
    wake_on: list[str] | None = None,
    fallback: bool | None = None,
) -> None:
    P.spawn_pai(
        pid=pid,
        slug=slug,
        description=f"{slug} test",
        wake_on=wake_on,
        fallback=fallback,
    )


def test_route_explicit_match_excludes_fallback(live_dir: Path) -> None:
    # root claims kernel:*, pai is fallback → only root fires.
    _spawn("root", pid=1, wake_on=["kernel:*"])
    _spawn("pai", pid=2, fallback=True)
    assert M._route_to_pids("kernel:reload_failed") == [1]


def test_route_falls_through_to_fallback(live_dir: Path) -> None:
    _spawn("root", pid=1, wake_on=["kernel:*"])
    _spawn("pai", pid=2, fallback=True)
    # Nothing matches imessage:new → fallback PAI fires.
    assert M._route_to_pids("imessage:new") == [2]


def test_route_multiple_explicit_fanout(live_dir: Path) -> None:
    _spawn("a", pid=3, wake_on=["imessage:*"])
    _spawn("b", pid=4, wake_on=["imessage:new"])
    _spawn("pai", pid=2, fallback=True)
    # Both a and b match; fallback is suppressed.
    assert M._route_to_pids("imessage:new") == [3, 4]


def test_route_no_fallback_uses_default_pid(live_dir: Path) -> None:
    _spawn("root", pid=1, wake_on=["kernel:*"])
    # No fallback PAI, no match → default fallback_pid.
    assert M._route_to_pids("imessage:new", fallback_pid=7) == [7]


def test_route_skips_non_running_fallback(live_dir: Path) -> None:
    _spawn("root", pid=1, wake_on=["kernel:*"])
    _spawn("pai", pid=2, fallback=True)
    P.resolve("pai", "cancelled")
    assert M._route_to_pids("imessage:new", fallback_pid=99) == [99]


def test_route_multiple_fallbacks_all_fire(live_dir: Path) -> None:
    _spawn("a", pid=2, fallback=True)
    _spawn("b", pid=3, fallback=True)
    assert M._route_to_pids("imessage:new") == [2, 3]


def test_route_fallback_with_wake_on_match(live_dir: Path) -> None:
    # A PAI can have both wake_on and fallback. If wake_on matches it
    # fires via wake_on — fallback is only used when *no one else* matched.
    _spawn("a", pid=2, wake_on=["imessage:*"], fallback=True)
    assert M._route_to_pids("imessage:new") == [2]
    assert M._route_to_pids("kernel:foo") == [2]  # via fallback path


def test_build_system_prompt_custom_block_from_prompt_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    role = tmp_path / "role.md"
    role.write_text("you are the test role\n")
    monkeypatch.setattr(bootstrap, "REPO_ROOT", tmp_path, raising=True)
    monkeypatch.setattr(bootstrap, "PAI_ROOT", tmp_path, raising=True)
    out = bootstrap.build_system_prompt(pai=2, prompt_path="role.md", boilerplate=[])
    assert "<custom>\nyou are the test role\n</custom>" in out


def test_build_system_prompt_custom_block_from_prompt_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdir = tmp_path / "myrole"
    pdir.mkdir()
    (pdir / "a-intro.md").write_text("intro body\n")
    (pdir / "b-rules.md").write_text("rules body\n")
    monkeypatch.setattr(bootstrap, "REPO_ROOT", tmp_path, raising=True)
    monkeypatch.setattr(bootstrap, "PAI_ROOT", tmp_path, raising=True)
    out = bootstrap.build_system_prompt(pai=2, prompt_dir="myrole", boilerplate=[])
    # Files concatenated in sorted order inside a single <custom> block.
    assert "<custom>" in out and "</custom>" in out
    assert out.index("intro body") < out.index("rules body")


def test_build_system_prompt_identity_overlay_appends_after_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Base persona ships in the bundle prompt_dir; the writable identity
    # overlay concatenates AFTER it inside the same <custom> block, so
    # later prose (the librarian's) can override the base.
    base = tmp_path / "myrole"
    base.mkdir()
    (base / "00-role.md").write_text("base persona\n")
    overlay = tmp_path / "prompt"
    overlay.mkdir()
    (overlay / "50-identity.md").write_text("evolved identity\n")
    (overlay / "90-override.md").write_text("override line\n")
    monkeypatch.setattr(bootstrap, "REPO_ROOT", tmp_path, raising=True)
    monkeypatch.setattr(bootstrap, "PAI_ROOT", tmp_path, raising=True)
    out = bootstrap.build_system_prompt(
        pai=2, prompt_dir="myrole", identity_dir=str(overlay), boilerplate=[]
    )
    assert "<custom>" in out and "</custom>" in out
    # Base first, then overlay files in sorted order — overrides land last.
    assert out.index("base persona") < out.index("evolved identity")
    assert out.index("evolved identity") < out.index("override line")


def test_build_system_prompt_identity_overlay_absent_is_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A missing overlay dir contributes nothing (subagents, un-seeded roots).
    base = tmp_path / "myrole"
    base.mkdir()
    (base / "00-role.md").write_text("base persona\n")
    monkeypatch.setattr(bootstrap, "REPO_ROOT", tmp_path, raising=True)
    monkeypatch.setattr(bootstrap, "PAI_ROOT", tmp_path, raising=True)
    out = bootstrap.build_system_prompt(
        pai=2,
        prompt_dir="myrole",
        identity_dir=str(tmp_path / "does-not-exist"),
        boilerplate=[],
    )
    assert "base persona" in out


def test_build_user_turn_renders_sender_verbatim() -> None:
    # build_user_turn no longer adds its own "pai:" prefix — callers pass
    # the fully-formatted handle. This lets nudge.py distinguish subagent
    # senders ("subagent:7") from generic PAI peers ("pai:42").
    out = bootstrap.build_user_turn("subagent response", sender="subagent:7")
    assert "from: subagent:7" in out
    out = bootstrap.build_user_turn("peer message", sender="pai:42")
    assert "from: pai:42" in out


def test_boilerplate_default_picks_per_role(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Stage all three default boilerplate files and verify the kernel-level
    # defaults: root → owner only; fleet pai → owner + memory-usage +
    # capability-escalation; subagent → owner + capability-escalation.
    bp = tmp_path / "etc" / "boilerplate"
    bp.mkdir(parents=True)
    (bp / "owner.md").write_text("OWNER BODY\n")
    (bp / "memory-usage.md").write_text("MEMORY BODY\n")
    (bp / "capability-escalation.md").write_text("ESC BODY\n")
    monkeypatch.setattr(bootstrap, "PAI_ROOT", tmp_path, raising=True)
    monkeypatch.setattr(bootstrap, "REPO_ROOT", tmp_path, raising=True)

    out_default = bootstrap.build_system_prompt(pai=2, prompt_path=None)
    assert "<owner>" in out_default
    assert "<memory-usage>" in out_default
    assert "<capability-escalation>" in out_default

    out_root = bootstrap.build_system_prompt(pai=1, prompt_path=None)
    assert "<owner>" in out_root
    assert "<capability-escalation>" not in out_root
    assert "<memory-usage>" not in out_root

    out_subagent = bootstrap.build_system_prompt(pai=7, parent=2, prompt_path=None)
    assert "<capability-escalation>" in out_subagent
    assert "<memory-usage>" not in out_subagent


def test_shipped_subagent_prompt_requires_done_result() -> None:
    prompt = Path("src/prompts/subagent.md").read_text()

    assert "subagent done --result result.md" in prompt
    assert "Do **not** end a completed task with plain assistant text" in prompt
    assert "Self-termination goes through `done --result`" in prompt


def test_boilerplate_explicit_list_renders_in_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bp = tmp_path / "etc" / "boilerplate"
    bp.mkdir(parents=True)
    (bp / "alpha.md").write_text("A\n")
    (bp / "beta.md").write_text("B\n")
    monkeypatch.setattr(bootstrap, "PAI_ROOT", tmp_path, raising=True)
    monkeypatch.setattr(bootstrap, "REPO_ROOT", tmp_path, raising=True)
    out = bootstrap.build_system_prompt(pai=2, boilerplate=["beta", "alpha"])
    assert out.index("<beta>") < out.index("<alpha>")


def test_boilerplate_missing_file_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bootstrap, "PAI_ROOT", tmp_path, raising=True)
    with pytest.raises(FileNotFoundError):
        bootstrap.build_system_prompt(pai=2, boilerplate=["does-not-exist"])


def test_build_system_prompt_no_custom_when_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bootstrap, "REPO_ROOT", tmp_path, raising=True)
    monkeypatch.setattr(bootstrap, "PAI_ROOT", tmp_path, raising=True)
    out_none = bootstrap.build_system_prompt(pai=1, prompt_path=None, boilerplate=[])
    assert "<custom>" not in out_none
    out_missing = bootstrap.build_system_prompt(
        pai=1, prompt_path="does/not/exist.md", boilerplate=[]
    )
    assert "<custom>" not in out_missing


def test_owner_profile_block_present_for_fleet_and_subagent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The canonical owner profile is injected for every PAI — fleet members,
    # subagents, and root alike. They all act on the owner's behalf, so the
    # owner's preferences and key people are load-bearing context everywhere.
    profile = tmp_path / "var" / "lib" / "owner" / "profile.md"
    profile.parent.mkdir(parents=True)
    profile.write_text("# Owner\nName: Sam\nTimezone: PT\n")
    monkeypatch.setattr(bootstrap, "PAI_ROOT", tmp_path, raising=True)
    monkeypatch.setattr(bootstrap, "REPO_ROOT", tmp_path, raising=True)

    out_fleet = bootstrap.build_system_prompt(pai=2, prompt_path=None, boilerplate=[])
    assert "<owner-profile>" in out_fleet
    assert "Name: Sam" in out_fleet

    out_subagent = bootstrap.build_system_prompt(
        pai=7, parent=2, prompt_path=None, boilerplate=[]
    )
    assert "<owner-profile>" in out_subagent

    out_root = bootstrap.build_system_prompt(pai=1, prompt_path=None, boilerplate=[])
    assert "<owner-profile>" in out_root


def test_owner_profile_block_absent_when_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No file → the block vanishes entirely (no empty shell).
    monkeypatch.setattr(bootstrap, "PAI_ROOT", tmp_path, raising=True)
    monkeypatch.setattr(bootstrap, "REPO_ROOT", tmp_path, raising=True)
    out = bootstrap.build_system_prompt(pai=2, prompt_path=None, boilerplate=[])
    assert "<owner-profile>" not in out

    # An empty/whitespace-only file is also treated as absent.
    profile = tmp_path / "var" / "lib" / "owner" / "profile.md"
    profile.parent.mkdir(parents=True)
    profile.write_text("   \n\n")
    out_empty = bootstrap.build_system_prompt(pai=2, prompt_path=None, boilerplate=[])
    assert "<owner-profile>" not in out_empty


def _block(out: str, tag: str) -> str:
    start = out.index(f"<{tag}>")
    end = out.index(f"</{tag}>", start)
    return out[start:end]


def test_parent_prompt_hides_bin_that_collides_with_system_subagent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "pai"
    home = root / "home" / "pai"
    (home / "bin").mkdir(parents=True)
    (home / "memory" / "skills").mkdir(parents=True)
    for name in ("browse", "subagent"):
        (home / "bin" / name).write_text("")
    subagent_dir = root / "usr" / "lib" / "subagents" / "browse"
    subagent_dir.mkdir(parents=True)
    (subagent_dir / "package.yaml").write_text(
        "name: browse\n"
        "kind: subagent\n"
        "description: Drives Chrome through a child process.\n"
    )
    (root / "usr" / "lib" / "skills").mkdir(parents=True)
    (root / "proc").mkdir(parents=True)

    monkeypatch.setattr(paths, "PAI_ROOT", root, raising=True)
    monkeypatch.setattr(bootstrap, "PAI_ROOT", root, raising=True)
    monkeypatch.setattr(bootstrap, "REPO_ROOT", root, raising=True)
    monkeypatch.setattr(bootstrap, "PROC_DIR", root / "proc", raising=True)

    out = bootstrap.build_system_prompt(
        pai=2,
        parent=None,
        home_dir=str(home),
        boilerplate=[],
    )

    bin_block = _block(out, "bin")
    assert "\nbrowse\n" not in bin_block
    assert "\nsubagent\n" in bin_block
    assert "browse: Drives Chrome through a child process." in _block(
        out, "system-subagents"
    )


def test_parent_prompt_lists_persub_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "pai"
    home = root / "home" / "pai"
    home.mkdir(parents=True)
    (root / "proc").mkdir(parents=True)
    (root / "usr" / "lib" / "skills").mkdir(parents=True)
    (root / "usr" / "lib" / "subagents").mkdir(parents=True)

    monkeypatch.setattr(paths, "PAI_ROOT", root, raising=True)
    monkeypatch.setattr(P, "PROC_DIR", root / "proc", raising=True)
    monkeypatch.setattr(P, "HOME_DIR", home, raising=True)
    monkeypatch.setattr(bootstrap, "PAI_ROOT", root, raising=True)
    monkeypatch.setattr(bootstrap, "REPO_ROOT", root, raising=True)
    monkeypatch.setattr(bootstrap, "PROC_DIR", root / "proc", raising=True)

    P.spawn_pai(pid=2, slug="pai", description="parent")
    P.spawn_pai(
        pid=5,
        slug="pai.computer-use",
        description="local macOS computer-use operator for app automation",
        parent=2,
        extra={"persistent": True, "persub": True},
    )

    out = bootstrap.build_system_prompt(
        pai=2,
        parent=None,
        home_dir=str(home),
        boilerplate=[],
    )

    assert (
        "pid 5  pai.computer-use: local macOS computer-use operator for app automation"
        in _block(out, "my-persubs")
    )


def test_fleet_prompt_uses_compact_fhs_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "pai"
    home = root / "home" / "pai"
    home.mkdir(parents=True)
    (root / "proc").mkdir(parents=True)
    (root / "usr" / "lib" / "skills").mkdir(parents=True)
    (root / "usr" / "lib" / "subagents").mkdir(parents=True)

    monkeypatch.setattr(bootstrap, "PAI_ROOT", root, raising=True)
    monkeypatch.setattr(bootstrap, "REPO_ROOT", root, raising=True)
    monkeypatch.setattr(bootstrap, "PROC_DIR", root / "proc", raising=True)

    out = bootstrap.build_system_prompt(
        pai=2,
        parent=None,
        home_dir=str(home),
        boilerplate=[],
    )

    assert "<fhs-reference>" in out
    assert str(home) in _block(out, "fhs-reference")
    assert "<home-fhs>" not in out
    assert "<system-fhs>" not in out


def test_subagent_prompt_keeps_bin_that_collides_with_system_subagent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "pai"
    home = root / "home" / "browse-2026-05-23"
    (home / "bin").mkdir(parents=True)
    (home / "memory" / "skills").mkdir(parents=True)
    (home / "bin" / "browse").write_text("")
    subagent_dir = root / "usr" / "lib" / "subagents" / "browse"
    subagent_dir.mkdir(parents=True)
    (subagent_dir / "package.yaml").write_text(
        "name: browse\n"
        "kind: subagent\n"
        "description: Drives Chrome through a child process.\n"
    )
    (root / "usr" / "lib" / "skills").mkdir(parents=True)
    prompts = root / "usr" / "share" / "prompts"
    prompts.mkdir(parents=True)
    (prompts / "subagent.md").write_text("subagent parent {parent}\n")
    (root / "proc").mkdir(parents=True)

    monkeypatch.setattr(paths, "PAI_ROOT", root, raising=True)
    monkeypatch.setattr(bootstrap, "PAI_ROOT", root, raising=True)
    monkeypatch.setattr(bootstrap, "REPO_ROOT", root, raising=True)
    monkeypatch.setattr(bootstrap, "PROC_DIR", root / "proc", raising=True)

    out = bootstrap.build_system_prompt(
        pai=7,
        parent=2,
        home_dir=str(home),
        boilerplate=[],
    )

    assert "\nbrowse\n" in _block(out, "bin")
    assert "<system-subagents>" not in out


def test_capabilities_block_reflects_grant(monkeypatch: pytest.MonkeyPatch) -> None:
    from boot import config as C

    monkeypatch.setattr(
        bootstrap, "_mounted_for_pai", lambda pai: ("email-pai", {"email"})
    )
    monkeypatch.setattr(
        C, "capability_modes",
        lambda path=None: {"email_send": "yes", "imessage_send": "no"},
    )
    out = bootstrap._capabilities_block(7)
    assert "<capabilities>" in out
    assert "Email — SEND GRANTED" in out
    # imessage driver not mounted → not surfaced for this PAI.
    assert "iMessage" not in out


def test_capabilities_block_ask_is_approval_required(monkeypatch: pytest.MonkeyPatch) -> None:
    from boot import config as C

    monkeypatch.setattr(
        bootstrap, "_mounted_for_pai", lambda pai: ("email-pai", {"email"})
    )
    monkeypatch.setattr(
        C, "capability_modes",
        lambda path=None: {"email_send": "ask", "imessage_send": "no"},
    )
    out = bootstrap._capabilities_block(7)
    assert "Email — APPROVAL REQUIRED" in out
    assert "action: send" in out


def test_capabilities_block_denied_is_drafts_only(monkeypatch: pytest.MonkeyPatch) -> None:
    from boot import config as C

    monkeypatch.setattr(
        bootstrap, "_mounted_for_pai", lambda pai: ("email-pai", {"email"})
    )
    monkeypatch.setattr(
        C, "capability_modes",
        lambda path=None: {"email_send": "no", "imessage_send": "no"},
    )
    out = bootstrap._capabilities_block(7)
    assert "Email — DRAFTS ONLY" in out


def test_capabilities_block_empty_without_channel_drivers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A PAI that mounts no channel driver (e.g. root) gets no block at all.
    monkeypatch.setattr(bootstrap, "_mounted_for_pai", lambda pai: ("root", set()))
    assert bootstrap._capabilities_block(1) == ""
