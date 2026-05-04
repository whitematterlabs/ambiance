"""Nudge — the single entrypoint for waking PAI.

Assembles the bootstrap (system prompt + user turn) and runs one LLM
turn against the filesystem. Loads the target PAI's prior conversation
history from proc/<pai>/messages.jsonl, threads it through the turn,
and persists the updated history back on completion.

Emits two pointer-style events per turn:
  * ``pai:<slug>:input``  — before the LLM runs, with reason/trigger.
  * ``pai:<slug>:output`` — after history is committed, pointing at
    the last line of messages.jsonl (turn_index, messages_path).

Listeners subscribe via ``wake_on:`` in /etc/config.yaml. Avoid
wildcard subscriptions like ``pai:*:output`` — the listener's own
output would re-wake it. Target specific slugs.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import time
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from . import bootstrap, llm, stitch, tokens
from . import processes as P
from .processes import HOME_DIR, ProcessNotFound, append_log


# Default per-PAI prompt-window threshold (tokens). Once
# `last_window_tokens` for a PAI crosses this, the next nudge to it is
# preceded by a kernel-issued compact nudge. Override per-PAI with
# `compact_threshold:` in /etc/config.yaml.
DEFAULT_COMPACT_THRESHOLD = 150_000

# Cooldown after a compaction attempt: don't re-trigger compaction for
# the same PAI again within this window even if tokens still exceed the
# threshold. Insurance against an infinite compact loop if the PAI
# fails to actually call bin/compact during the compact turn.
_COMPACT_COOLDOWN_SECS = 30.0

# Per-slug FIFO serialization. Other concurrent nudges to the same PAI
# block on this lock — which IS the queue. asyncio.Lock guarantees fair
# wake order, giving us drain-in-order for free.
_pai_locks: dict[str, asyncio.Lock] = {}
_recently_compacted: dict[str, float] = {}


def _slug_lock(slug: str) -> asyncio.Lock:
    lock = _pai_locks.get(slug)
    if lock is None:
        lock = asyncio.Lock()
        _pai_locks[slug] = lock
    return lock


_COMPACT_INSTRUCTION = (
    "Your conversation history has grown past its compaction threshold. "
    "Summarize the conversation so far for context compaction and call "
    "`bin/compact \"<your summary>\"` to apply it. Keep the summary "
    "focused on what matters for the next nudge: open loops, recent "
    "decisions, who said what — not verbatim transcripts. After this "
    "turn the kernel will archive the full history and replace the live "
    "conversation with your summary."
)


def _history_path(pai_slug: str) -> Path:
    return HOME_DIR / "proc" / pai_slug / "messages.jsonl"


def _load_history(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        out.append(json.loads(line))
    return out


def _append_to_me_thread(pai_pid: int, text: str) -> None:
    """Post PAI's reply to today's me/<pid>/<date>.md.

    Format: `[HH:MM] pai: <body>\\n` where body may span multiple lines
    (markdown survives intact). Readers anchor message boundaries on the
    `[HH:MM] sender:` line prefix, not on blank lines, so paragraph
    separators inside the body don't fragment the message."""
    day = date.today().isoformat()
    path = HOME_DIR / "communication" / "messages" / "me" / str(pai_pid) / f"{day}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    hm = datetime.now().strftime("%H:%M")
    body = text.strip()
    if not body:
        return
    with path.open("a", encoding="utf-8") as f:
        f.write(f"[{hm}] pai: {body}\n")


def _save_history(path: Path, messages: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = "".join(json.dumps(m) + "\n" for m in messages)
    # tmp-file + rename for atomicity.
    fd, tmp = tempfile.mkstemp(prefix=".messages.", suffix=".jsonl", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as f:
            f.write(data)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def apply_pending_history_action(pai_slug: str) -> bool:
    """Public wrapper — apply any queued clear/compact for `pai_slug` immediately.

    Intended for callers (e.g. the TUI) that run bin/clear or bin/compact
    outside a PAI turn and need the action applied synchronously.
    """
    return _apply_history_action(pai_slug, _history_path(pai_slug))


def _apply_history_action(pai_slug: str, history_path: Path) -> bool:
    """If PAI queued a clear/compact via `bin/clear` or `bin/compact` during
    the turn, apply it now: archive the just-saved history and rewrite the
    live jsonl. Returns True if an action was applied."""
    proc_dir = HOME_DIR / "proc" / pai_slug
    action_path = proc_dir / ".history-action"
    if not action_path.exists():
        return False

    raw = action_path.read_text()
    action_path.unlink()
    lines = raw.splitlines()
    action = lines[0].strip() if lines else ""

    archive_dir = proc_dir / "history"
    archive_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%dT%H%M%S")
    archive_path = archive_dir / f"{ts}-{action or 'unknown'}.jsonl"
    if history_path.exists():
        shutil.copy(history_path, archive_path)

    rel_archive = archive_path.relative_to(HOME_DIR)

    if action == "clear":
        _save_history(history_path, [])
        try:
            append_log(pai_slug, f"context cleared — archived to {rel_archive}")
        except ProcessNotFound:
            pass
        print(f"[kernel] cleared pai={pai_slug} context — archived to {rel_archive}", flush=True)
    elif action == "compact":
        summary = "\n".join(lines[1:]).strip() or "(no summary provided)"
        compacted = [
            {"role": "user", "content": f"[compacted prior context]\n{summary}"},
            {"role": "assistant", "content": "Understood. Continuing."},
        ]
        _save_history(history_path, compacted)
        try:
            append_log(pai_slug, f"context compacted — archived to {rel_archive}")
        except ProcessNotFound:
            pass
        print(f"[kernel] compacted pai={pai_slug} context — archived to {rel_archive}", flush=True)
    else:
        try:
            append_log(pai_slug, f"ignored unknown history-action: {action!r}")
        except ProcessNotFound:
            pass
        print(f"[kernel] ignored unknown history-action {action!r} (pai={pai_slug})", flush=True)
        return False
    return True


async def nudge(
    reason: str,
    slug: Optional[str] = None,
    context: Optional[dict] = None,
    to: int = 1,
    from_: Optional[int] = None,
    from_kind: str = "pai",
    _exempt: bool = False,
) -> None:
    header = f"[kernel] nudge: {reason}"
    if slug:
        header += f" ({slug})"
    print(header, flush=True)

    pai_pid = int(to)
    try:
        pai_slug = P.find_pai_slug(pai_pid)
    except ProcessNotFound:
        print(f"[kernel] nudge: no PAI with pid={pai_pid}", flush=True)
        return

    if _exempt:
        await _nudge_locked(reason, slug, context, pai_pid, pai_slug, from_, from_kind)
        return

    async with _slug_lock(pai_slug):
        # Threshold check runs inside the lock so concurrent nudges queue
        # behind a compact-in-progress and re-evaluate after it finishes.
        last_window = tokens.read_last_window(pai_slug)
        if last_window is not None:
            try:
                pai_spec = P.read_spec(pai_slug)
            except ProcessNotFound:
                pai_spec = {}
            threshold = pai_spec.get("compact_threshold") or DEFAULT_COMPACT_THRESHOLD
            cooled = (time.monotonic() - _recently_compacted.get(pai_slug, 0.0)
                      < _COMPACT_COOLDOWN_SECS)
            if last_window >= threshold and not cooled:
                _recently_compacted[pai_slug] = time.monotonic()
                try:
                    append_log(
                        pai_slug,
                        f"kernel: compacting (last_window={last_window} >= {threshold})",
                    )
                except ProcessNotFound:
                    pass
                print(
                    f"[kernel] compaction: pai={pai_slug} "
                    f"last_window={last_window} threshold={threshold}",
                    flush=True,
                )
                await _nudge_locked(
                    "kernel:compact",
                    None,
                    {"instruction": _COMPACT_INSTRUCTION,
                     "last_window_tokens": last_window,
                     "threshold": threshold},
                    pai_pid, pai_slug, None, "kernel",
                )

        await _nudge_locked(reason, slug, context, pai_pid, pai_slug, from_, from_kind)


async def _nudge_locked(
    reason: str,
    slug: Optional[str],
    context: Optional[dict],
    pai_pid: int,
    pai_slug: str,
    from_: Optional[int],
    from_kind: str,
) -> None:
    log_line = f"nudge: {reason}" + (f" ({slug})" if slug else "")
    try:
        append_log(pai_slug, log_line)
    except ProcessNotFound:
        pass

    try:
        P.mark_busy(pai_slug, log_line)
    except ProcessNotFound:
        pass

    try:
        await _nudge_body(reason, slug, context, pai_pid, pai_slug, from_, from_kind)
    finally:
        P.clear_busy(pai_slug)


async def _nudge_body(
    reason: str,
    slug: Optional[str],
    context: Optional[dict],
    pai_pid: int,
    pai_slug: str,
    from_: Optional[int],
    from_kind: str,
) -> None:
    if slug and slug != pai_slug:
        try:
            append_log(slug, f"kernel: nudge — {reason}")
        except ProcessNotFound:
            pass

    # Announce turn start. Listeners with wake_on: [pai:<slug>:input]
    # can react before the LLM runs (rare, but symmetric with :output).
    input_payload = {
        "source": "pai",
        "kind": f"pai:{pai_slug}:input",
        "slug": pai_slug,
        "pid": pai_pid,
        "reason": reason,
    }
    if context is not None:
        input_payload["trigger"] = context
    P.emit_event(input_payload)

    try:
        pai_spec = P.read_spec(pai_slug)
    except ProcessNotFound:
        pai_spec = {}
    parent = pai_spec.get("parent")
    parent_pid = int(parent) if parent is not None else None
    parent_str = str(parent_pid) if parent_pid is not None else None
    # Persistent PAIs (config-declared fleet members) live forever — they
    # have a parent for delegation/return-routing, but a single nudge is
    # not their full lifetime. Only ephemeral subagents resolve on turn end.
    is_ephemeral = parent_str is not None and not pai_spec.get("persistent")

    system = bootstrap.build_system_prompt(
        pai=pai_pid,
        parent=parent_pid,
        prompt_path=pai_spec.get("prompt"),
        home_dir=str(stitch.home_for(pai_slug)),
        persub=bool(pai_spec.get("persub")),
    )
    sender = f"{from_kind}:{from_}" if from_ is not None else None
    user = bootstrap.build_user_turn(reason, slug, context, sender=sender)

    history_path = _history_path(pai_slug)
    history = _load_history(history_path)

    env = {
        "PAI_SLUG": pai_slug,
        "PAI_PID": str(pai_pid),
        "PAI_PARENT": parent_str or "",
    }

    try:
        reply, new_history = await llm.run_turn(
            system,
            user,
            history=history,
            env=env,
            provider=pai_spec.get("provider"),
            model=pai_spec.get("model"),
        )
    except llm.TurnCancelled as c:
        _save_history(history_path, c.messages)
        print(f"[kernel] nudge interrupted (pai={pai_slug})", flush=True)
        try:
            append_log(pai_slug, "nudge interrupted by owner")
        except ProcessNotFound:
            pass
        if slug and slug != pai_slug:
            try:
                append_log(slug, "kernel: nudge interrupted")
            except ProcessNotFound:
                pass
        if is_ephemeral:
            try:
                P.resolve(pai_slug, "cancelled")
            except ProcessNotFound:
                pass
        return
    except Exception as e:
        print(f"[kernel] nudge failed: {e!r}", flush=True)
        try:
            append_log(pai_slug, f"nudge failed — {e!r}")
        except ProcessNotFound:
            pass
        if slug and slug != pai_slug:
            try:
                append_log(slug, f"kernel: nudge failed — {e!r}")
            except ProcessNotFound:
                pass
        if is_ephemeral:
            try:
                P.resolve(pai_slug, "failed")
            except ProcessNotFound:
                pass
        # Surface the failure to root so it can decide what to do.
        # Root itself failing has nowhere to escalate — just stop.
        if pai_pid != 1:
            await nudge(
                reason="nudge failed",
                slug=pai_slug,
                context={
                    "target": pai_slug,
                    "target_pid": pai_pid,
                    "original_reason": reason,
                    "error": repr(e),
                },
                to=1,
                from_=pai_pid,
                from_kind="pai",
            )
        return

    _save_history(history_path, new_history)

    # Announce turn end. Subscribers (e.g. memory PAI) re-read the
    # last line of messages_path themselves — payload is a pointer,
    # not the content. Loop hazard: a listener subscribed via
    # `pai:*:output` will self-trigger on its own turns; target
    # specific slugs (e.g. `pai:main:output`) instead.
    P.emit_event({
        "source": "pai",
        "kind": f"pai:{pai_slug}:output",
        "slug": pai_slug,
        "pid": pai_pid,
        "turn_index": len(new_history),
        "messages_path": f"proc/{pai_slug}/messages.jsonl",
    })

    _apply_history_action(pai_slug, history_path)

    if reply:
        print(f"[pai:{pai_slug}] {reply}", flush=True)
        # Top-level fleet PAIs (no parent) write back to the owner's
        # me-thread so the TUI chat tab shows their replies. Persubs
        # are also owner-addressable (the user opens a tab for them
        # in the TUI), so their replies belong in the me-thread too.
        # Plain ephemeral subagents talk to their parent via
        # subagent:response, not the me-thread, so they're excluded.
        if not parent_str or pai_spec.get("persub"):
            _append_to_me_thread(pai_pid, reply)
    print("[kernel] nudge complete", flush=True)
    try:
        append_log(pai_slug, "nudge complete")
    except ProcessNotFound:
        pass
    if slug and slug != pai_slug:
        try:
            append_log(slug, "kernel: nudge complete")
        except ProcessNotFound:
            pass

    if is_ephemeral:
        try:
            P.resolve(pai_slug, "completed")
        except ProcessNotFound:
            pass
