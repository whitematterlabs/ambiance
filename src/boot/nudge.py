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
import traceback
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Optional

from . import bootstrap, config, debugger, llm, stitch, tokens
from . import paths as paths_mod
from . import processes as P
from .processes import HOME_DIR, PROC_DIR, ProcessNotFound, append_log


# Default per-PAI prompt-window threshold (tokens). Once
# `last_window_tokens` for a PAI crosses this, the next nudge to it is
# preceded by a kernel-issued compact nudge. Override per-PAI with
# `compact_threshold:` in /etc/config.yaml.
DEFAULT_COMPACT_THRESHOLD = 150_000

# Hardline (kernel-enforced) compaction threshold (tokens). The soft
# threshold above only *asks* the PAI to call `bin/compact` — it can be
# ignored by the model and is suppressed by the post-compaction cooldown,
# so a PAI flooded with nudges (or one whose single turn ingests a huge
# read) can keep climbing far past it. Once `last_window_tokens` crosses
# THIS line, the kernel compacts the history itself — no model
# cooperation, no cooldown — before delivering the next queued nudge.
# It's the backstop; the cooperative `bin/compact` path stays the
# graceful primary. Override per-PAI with `hard_compact_threshold:` in
# /etc/config.yaml.
DEFAULT_HARD_COMPACT_THRESHOLD = 400_000

# Post-turn skill-candidate trigger. After a non-trivial turn the kernel nudges
# librarian-pai to consider distilling the just-finished workflow into a SKILL.md
# (the procedural twin of `memorize`). Librarian is the sole skills writer.
SKILL_CANDIDATE_DURATION_SECS = 30
SKILL_CANDIDATE_TOOL_CALLS = 5
LIBRARIAN_SLUG = "librarian-pai"

OVERCLOCK_SENTINEL = "<PAI_OVERCLOCK_COMPLETE>"
OVERCLOCK_MAX_TURNS = 10
OVERCLOCK_MAX_SECONDS = 60 * 60

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


def _is_ad_hoc_subagent(spec: dict) -> bool:
    return (
        spec.get("kind") == "pai"
        and spec.get("parent") is not None
        and not spec.get("persub")
        and "run" not in spec
        and "schedule" not in spec
    )


def _proc_already_resolved(slug: str) -> bool:
    """True if the proc was resolved/reaped *during* its own turn.

    A subagent's standard exit (`bin/subagent done` / `reply --done`) and a
    parent `subagent kill` both resolve the child's proc — and reap it — from
    inside the turn that's now ending. The kernel's post-turn exit (auto-finish
    fallback + the ephemeral resolve) reads `pai_spec` captured at turn *start*,
    so without this guard it would race the child's own exit: re-deliver a
    duplicate `subagent:response` to the parent and then fail its now-redundant
    `P.resolve` with ProcessNotFound ("subagent auto-finish failed"). If the
    child already finished itself, the kernel has nothing left to do.
    """
    try:
        status = P.read_status(slug)
    except (ProcessNotFound, FileNotFoundError):
        # Proc dir reaped (or reaped then partially recreated by the
        # history write) — the child resolved itself.
        return True
    return status in P.TERMINAL_STATUSES


def _auto_finish_subagent_plain_reply(
    *,
    pai_slug: str,
    pai_pid: int,
    parent_pid: int,
    visible_reply: str,
) -> bool:
    """Last-resort handoff when a spawned child replies with plain text.

    Subagents are supposed to call `bin/subagent done --result result.md`.
    If a child instead ends its turn with normal assistant text, that text is
    otherwise invisible to the parent and the child stays `running` forever.
    Preserve the answer in the parent's workspace and deliver the normal done
    event so the parent can continue.
    """
    text = visible_reply.strip()
    if not text:
        return False
    try:
        parent_slug = P.find_pai_slug(parent_pid)
        result_dir = HOME_DIR / parent_slug / "workspace" / pai_slug
        result_dir.mkdir(parents=True, exist_ok=True)
        (result_dir / "result.md").write_text(f"{text}\n")
        result_ref = f"workspace/{pai_slug}/result.md"
        P.emit_event(
            {
                "source": "subagent",
                "kind": "subagent:response",
                "target_pid": parent_pid,
                "sender_pid": pai_pid,
                "text": (
                    "auto-fallback: child ended without calling "
                    f"`bin/subagent done`; saved plain reply to {result_ref}"
                ),
                "done": True,
                "result": result_ref,
                "auto_fallback": True,
            }
        )
        append_log(
            pai_slug,
            f"kernel: auto-finished plain subagent reply to {result_ref}",
        )
        P.resolve(pai_slug, "completed", notify_parent=False)
    except Exception as e:
        print(
            f"[kernel] subagent auto-finish failed for {pai_slug}: {e!r}",
            flush=True,
        )
        try:
            append_log(pai_slug, f"kernel: subagent auto-finish failed — {e!r}")
        except ProcessNotFound:
            pass
        return False
    return True


_COMPACT_INSTRUCTION = (
    "Your conversation history has grown past its compaction threshold. "
    "Summarize the conversation so far for context compaction and call "
    "`bin/compact \"<your summary>\"` to apply it. Keep the summary "
    "focused on what matters for the next nudge: open loops, recent "
    "decisions, who said what — not verbatim transcripts. After this "
    "turn the kernel will archive the full history and replace the live "
    "conversation with your summary."
)


_ONBOARDING_INSTRUCTION = (
    "First-run onboarding: you have not yet built the owner profile. Briefly "
    "tell the owner you're going to skim their last month of mail, messages, "
    "contacts, and calendar to get to know them. Then read and follow "
    "`memory/skills/operating/onboard-owner/SKILL.md`: read those sources, "
    "write the owner profile to the canonical absolute FHS path "
    "`/var/lib/owner/profile.md` (not a relative `var/lib/...` under your "
    "home), and end your turn with a short digest the owner can correct. If "
    "that skill file is missing, report that onboarding is not installed and "
    "do not improvise a profile. If there's almost nothing to read, say so "
    "and ask them to tell you about themselves instead."
)


def _is_overclock_context(context: Optional[dict]) -> bool:
    return isinstance(context, dict) and context.get("overclock") is True


def _overclock_goal(context: Optional[dict]) -> str:
    if not isinstance(context, dict):
        return ""
    goal = context.get("goal") or context.get("text") or ""
    return str(goal).strip()


def _overclock_instruction(context: Optional[dict]) -> str:
    goal = _overclock_goal(context)
    goal_block = f"\n\nGoal:\n{goal}" if goal else ""
    return (
        "Overclock mode is active. Keep working autonomously until the goal "
        "is genuinely complete or blocked by something that needs the owner. "
        "Use the tools available to you and continue across turns when more "
        "work is needed. Keep non-final owner-facing replies concise and do "
        f"not include `{OVERCLOCK_SENTINEL}` unless the goal is complete. "
        f"When the goal is complete, end your final reply with exactly "
        f"`{OVERCLOCK_SENTINEL}`."
        f"{goal_block}"
    )


def _strip_overclock_sentinel(reply: str) -> str:
    return reply.replace(OVERCLOCK_SENTINEL, "").strip()


def _history_path(pai_slug: str) -> Path:
    return HOME_DIR / "proc" / pai_slug / "messages.jsonl"


def _history_path_display(pai_slug: str) -> str:
    """Namespace-absolute path to a PAI's live transcript, as any PAI sees it.

    PAI_ROOT is mounted at `/` in every PAI's bash view, but each PAI's cwd is
    its *own* home. The kernel writes transcripts under the default PAI's
    stitched home (`HOME_DIR/proc/<slug>/`), NOT the FHS `/proc` — so a bare
    relative `proc/<slug>/messages.jsonl` resolves against the reader's home
    (e.g. librarian's, which has no `proc/`) and misses. Hand out the absolute
    path so any PAI can open it regardless of cwd."""
    return "/" + str(_history_path(pai_slug).relative_to(paths_mod.PAI_ROOT))


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


def _reset_window_gauge(proc_dir: Path) -> None:
    """Zero `last_window_tokens` in a PAI's token rollup. Called whenever the
    live history is reset/compacted out from under the gauge so the next
    threshold check reads the post-reset reality, not the stale pre-reset
    count. Best-effort — a malformed/absent tokens file is not fatal."""
    tokens_path = proc_dir / "tokens"
    if not tokens_path.exists():
        return
    try:
        data = json.loads(tokens_path.read_text())
        data["last_window_tokens"] = 0
        tokens_path.write_text(json.dumps(data))
    except Exception:
        pass


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


def _clear_history(pai_slug: str, history_path: Path, label: str = "clear") -> Optional[Path]:
    """Archive the live history jsonl, reset it to empty, and zero the token
    rollup's window gauge. Shared by the bin/clear action and kernel-driven
    resets (onboarding completion). Returns the archive path, or None if there
    was no history to archive."""
    proc_dir = PROC_DIR / pai_slug
    archive_dir = proc_dir / "history"
    archive_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%dT%H%M%S")
    archive_path = archive_dir / f"{ts}-{label}.jsonl"
    archived = history_path.exists()
    if archived:
        shutil.copy(history_path, archive_path)
    _save_history(history_path, [])
    _reset_window_gauge(proc_dir)
    return archive_path if archived else None


def _apply_history_action(pai_slug: str, history_path: Path) -> bool:
    """If PAI queued a clear/compact via `bin/clear` or `bin/compact` during
    the turn, apply it now: archive the just-saved history and rewrite the
    live jsonl. Returns True if an action was applied."""
    proc_dir = PROC_DIR / pai_slug
    action_path = proc_dir / ".history-action"
    if not action_path.exists():
        return False

    raw = action_path.read_text()
    action_path.unlink()
    lines = raw.splitlines()
    action = lines[0].strip() if lines else ""

    if action == "clear":
        archive_path = _clear_history(pai_slug, history_path, "clear")
        rel_archive = (archive_path or proc_dir / "history").relative_to(PROC_DIR.parent)
        try:
            append_log(pai_slug, f"context cleared — archived to {rel_archive}")
        except ProcessNotFound:
            pass
        print(f"[kernel] cleared pai={pai_slug} context — archived to {rel_archive}", flush=True)
    elif action == "compact":
        archive_dir = proc_dir / "history"
        archive_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%dT%H%M%S")
        archive_path = archive_dir / f"{ts}-{action}.jsonl"
        if history_path.exists():
            shutil.copy(history_path, archive_path)
        rel_archive = archive_path.relative_to(PROC_DIR.parent)
        summary = "\n".join(lines[1:]).strip() or "(no summary provided)"
        compacted = [
            {"role": "user", "content": f"[compacted prior context]\n{summary}"},
            {"role": "assistant", "content": "Understood. Continuing."},
        ]
        _save_history(history_path, compacted)
        # Zero the window gauge — same as the clear path. `last_window_tokens`
        # still holds the pre-compact (huge) count until the next turn runs;
        # leaving it stale would make the next nudge's threshold check fire a
        # redundant compaction against history that's already tiny.
        _reset_window_gauge(proc_dir)
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


# Substrings that identify a provider "prompt exceeds the context window"
# error. Matched against the stringified exception so it works across SDKs
# (Anthropic BadRequestError, OpenAI-compatible 400, etc.) without importing
# provider exception types into the kernel.
_OVERFLOW_MARKERS = (
    "maximum context",          # "maximum context length is N tokens"
    "context length",
    "context_length_exceeded",  # OpenAI-style error code
    "prompt is too long",       # Anthropic-style
    "input is too long",
    "too many tokens",
)

# Substrings that identify a transient / systemic infrastructure failure
# (network blip, provider timeout, rate limit, overload). These are NOT
# actionable by root and, escalated per-failure, snowball into a nudge storm
# against pid 1 — so we log them and stop rather than re-nudging root. Context
# overflow counts as transient here too: we recover from it kernel-side, so
# there's nothing for root to act on.
_TRANSIENT_MARKERS = (
    "connection error",
    "timed out",
    "timeout",
    "rate limit",
    "ratelimit",
    "429",
    "overloaded",
    "503",
    "502",
    "temporarily unavailable",
)

# Brief backoff before the kernel retries a turn that hit a transient provider
# error (timeout / dropped connection / 429 / 5xx), so the retry doesn't slam a
# still-recovering upstream. One retry only — see the nudge() loop.
_TRANSIENT_RETRY_DELAY = 2.0


def _is_context_overflow(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return any(m in msg for m in _OVERFLOW_MARKERS)


def _is_transient(exc: BaseException) -> bool:
    if _is_context_overflow(exc):
        return True
    msg = str(exc).lower()
    return any(m in msg for m in _TRANSIENT_MARKERS)


def _emergency_reset_history(pai_slug: str, history_path: Path) -> str:
    """Kernel-side last resort when a turn overflows the provider's context
    window: archive the oversized history and replace it with an empty
    conversation so the next turn fits.

    Unlike the soft `bin/compact` path, this needs no cooperation from the
    model — it's what recovers a PAI whose context grew past the *hard* limit
    because the model never actually compacted. Self-calibrates to the real
    provider limit (it only fires on an observed overflow). Returns the archive
    path (relative to PAI_ROOT) for logging."""
    proc_dir = PROC_DIR / pai_slug
    archive_dir = proc_dir / "history"
    archive_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%dT%H%M%S")
    archive_path = archive_dir / f"{ts}-overflow.jsonl"
    if history_path.exists():
        shutil.copy(history_path, archive_path)
    _save_history(history_path, [])
    # Reset the token rollup so the soft-compaction threshold doesn't keep
    # firing against the now-stale (pre-reset) window size.
    _reset_window_gauge(proc_dir)
    try:
        return str(archive_path.relative_to(PROC_DIR.parent))
    except ValueError:
        return str(archive_path)


def _hard_compact_history(
    pai_slug: str, history_path: Path, last_window: int, hard_threshold: int
) -> str:
    """Kernel-enforced compaction at the hard threshold — runs between turns,
    needs no model cooperation, and bypasses the soft-path cooldown.

    Unlike `_emergency_reset_history` (which fires reactively on an observed
    provider overflow and wipes to empty), this fires *proactively* the moment
    the standing window crosses `hard_threshold`, and leaves a one-line
    breadcrumb in the user/assistant shape the cooperative `bin/compact` path
    uses — so role alternation stays valid and the PAI knows its prior context
    was dropped. The graceful primary remains the model calling `bin/compact`
    with a real summary; this only catches the runaway. Returns the archive
    path (relative to PAI_ROOT) for logging."""
    proc_dir = PROC_DIR / pai_slug
    archive_dir = proc_dir / "history"
    archive_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%dT%H%M%S")
    archive_path = archive_dir / f"{ts}-hardcompact.jsonl"
    if history_path.exists():
        shutil.copy(history_path, archive_path)
    breadcrumb = (
        f"[prior context kernel hard-compacted at {last_window} tokens "
        f"(exceeded {hard_threshold}) — summarize as you go and call "
        f"bin/compact yourself to avoid this]"
    )
    _save_history(history_path, [
        {"role": "user", "content": breadcrumb},
        {"role": "assistant", "content": "Understood. Continuing."},
    ])
    _reset_window_gauge(proc_dir)
    try:
        return str(archive_path.relative_to(PROC_DIR.parent))
    except ValueError:
        return str(archive_path)


async def _handle_turn_failure(
    e: BaseException,
    reason: str,
    slug: Optional[str],
    pai_pid: int,
    pai_slug: str,
    is_ephemeral: bool,
) -> None:
    tb = "".join(traceback.format_exception(type(e), e, e.__traceback__))
    print(f"[kernel] nudge failed: {e!r}\n{tb}", end="", flush=True)
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
    # Surface genuine, actionable failures to root so it can decide what to do.
    # Transient/systemic infrastructure errors (network blips, provider
    # timeouts, rate limits, context overflow) are NOT actionable and,
    # escalated per-failure, snowball into a nudge storm against pid 1 — so we
    # log them and stop. Root itself failing has nowhere to escalate.
    if pai_pid != 1 and not _is_transient(e):
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


async def nudge(
    reason: str,
    slug: Optional[str] = None,
    context: Optional[dict] = None,
    to: int = 1,
    from_: Optional[int] = None,
    from_kind: str = "pai",
    msg_id: Optional[str] = None,
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
        if msg_id:
            P.emit_ack(msg_id, {
                "kind": "pai_message:dropped",
                "msg_id": msg_id,
                "target_pid": pai_pid,
                "reason": "no PAI with pid",
            })
        return

    if msg_id:
        P.emit_ack(msg_id, {
            "kind": "pai_message:ack",
            "msg_id": msg_id,
            "target_pid": pai_pid,
            "slug": pai_slug,
        })

    if _exempt:
        await _nudge_locked(reason, slug, context, pai_pid, pai_slug, from_, from_kind)
        return

    async with _slug_lock(pai_slug):
        if _is_overclock_context(context):
            await _overclock_locked(
                reason, slug, context, pai_pid, pai_slug, from_, from_kind
            )
            return

        await _maybe_compact_locked(pai_pid, pai_slug)
        await _nudge_locked(reason, slug, context, pai_pid, pai_slug, from_, from_kind)


async def _maybe_compact_locked(pai_pid: int, pai_slug: str) -> None:
    # Threshold check runs inside the lock so concurrent nudges queue
    # behind a compact-in-progress and re-evaluate after it finishes.
    last_window = tokens.read_last_window(pai_slug)
    if last_window is None:
        return
    try:
        pai_spec = P.read_spec(pai_slug)
    except ProcessNotFound:
        pai_spec = {}
    threshold = pai_spec.get("compact_threshold") or DEFAULT_COMPACT_THRESHOLD
    hard_threshold = (pai_spec.get("hard_compact_threshold")
                      or DEFAULT_HARD_COMPACT_THRESHOLD)

    # Hardline path: the window has run away past anything the cooperative
    # `bin/compact` path could be trusted to recover. Compact kernel-side now,
    # no model cooperation and no cooldown — we're between turns (the prior
    # turn already released; later nudges are queued behind this lock), so this
    # is exactly "stop the PAI after its current turn and drain the queue
    # against a fresh context."
    if last_window >= hard_threshold:
        _recently_compacted[pai_slug] = time.monotonic()
        rel_archive = _hard_compact_history(
            pai_slug, _history_path(pai_slug), last_window, hard_threshold
        )
        try:
            append_log(
                pai_slug,
                f"kernel: hard-compacted (last_window={last_window} >= "
                f"{hard_threshold}) — archived to {rel_archive}",
            )
        except ProcessNotFound:
            pass
        print(
            f"[kernel] hard-compaction: pai={pai_slug} "
            f"last_window={last_window} hard_threshold={hard_threshold} "
            f"— archived to {rel_archive}",
            flush=True,
        )
        return

    cooled = (time.monotonic() - _recently_compacted.get(pai_slug, 0.0)
              < _COMPACT_COOLDOWN_SECS)
    if last_window < threshold or cooled:
        return
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


async def _overclock_locked(
    reason: str,
    slug: Optional[str],
    context: Optional[dict],
    pai_pid: int,
    pai_slug: str,
    from_: Optional[int],
    from_kind: str,
) -> None:
    started = time.monotonic()
    goal = _overclock_goal(context)
    base_context = dict(context or {})
    base_context["overclock"] = True
    base_context["goal"] = goal
    try:
        append_log(pai_slug, f"overclock started — max_turns={OVERCLOCK_MAX_TURNS}")
    except ProcessNotFound:
        pass

    for turn in range(1, OVERCLOCK_MAX_TURNS + 1):
        await _maybe_compact_locked(pai_pid, pai_slug)
        elapsed = int(time.monotonic() - started)
        turn_context = {
            **base_context,
            "overclock_turn": turn,
            "overclock_max_turns": OVERCLOCK_MAX_TURNS,
            "overclock_elapsed_seconds": elapsed,
        }
        turn_reason = reason if turn == 1 else "overclock continue"
        status_prefix = f"overclock: turn {turn}/{OVERCLOCK_MAX_TURNS}"
        reply = await _nudge_locked(
            turn_reason,
            slug,
            turn_context,
            pai_pid,
            pai_slug,
            from_,
            from_kind,
            status_prefix=status_prefix,
            reply_filter=_strip_overclock_sentinel,
        )
        if reply is None:
            return
        if OVERCLOCK_SENTINEL in reply:
            try:
                append_log(pai_slug, f"overclock complete on turn {turn}")
            except ProcessNotFound:
                pass
            return
        if time.monotonic() - started >= OVERCLOCK_MAX_SECONDS:
            note = (
                "Overclock stopped after 60 minutes without a completion signal. "
                "Send a new Overclock goal to continue."
            )
            _append_to_me_thread(pai_pid, note)
            try:
                append_log(pai_slug, "overclock stopped at wall-clock limit")
            except ProcessNotFound:
                pass
            return

    note = (
        f"Overclock stopped after {OVERCLOCK_MAX_TURNS} turns without a "
        "completion signal. Send a new Overclock goal to continue."
    )
    _append_to_me_thread(pai_pid, note)
    try:
        append_log(pai_slug, "overclock stopped at turn limit")
    except ProcessNotFound:
        pass


async def _nudge_locked(
    reason: str,
    slug: Optional[str],
    context: Optional[dict],
    pai_pid: int,
    pai_slug: str,
    from_: Optional[int],
    from_kind: str,
    *,
    status_prefix: Optional[str] = None,
    reply_filter: Optional[Callable[[str], str]] = None,
) -> Optional[str]:
    log_line = f"nudge: {reason}" + (f" ({slug})" if slug else "")
    try:
        append_log(pai_slug, log_line)
    except ProcessNotFound:
        pass

    try:
        P.mark_busy(pai_slug, status_prefix or log_line)
    except ProcessNotFound:
        pass

    try:
        return await _nudge_body(
            reason,
            slug,
            context,
            pai_pid,
            pai_slug,
            from_,
            from_kind,
            status_prefix=status_prefix,
            reply_filter=reply_filter,
        )
    finally:
        P.clear_busy(pai_slug)


def _count_tool_calls(messages: list[dict]) -> int:
    """Count `tool_use` blocks across a slice of conversation messages.

    Assistant turns store content as a list of blocks; each tool invocation is
    a `{"type": "tool_use", ...}` block. String-content messages (plain user/
    assistant text) contribute nothing."""
    total = 0
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        total += sum(
            1
            for b in content
            if isinstance(b, dict) and b.get("type") == "tool_use"
        )
    return total


def _is_skill_candidate(pai_slug: str, duration: float, tool_calls: int) -> bool:
    """Whether a just-finished turn is worth offering to librarian as a skill.

    Loop guard is mandatory: librarian's own turns hit this same post-turn path,
    so it must never nominate itself (that would wake it on its own output and
    spin). A turn qualifies when it ran long or fanned out across many tools —
    the cheap signal that a reusable multi-step procedure just happened."""
    if pai_slug == LIBRARIAN_SLUG:
        return False
    return (
        duration > SKILL_CANDIDATE_DURATION_SECS
        or tool_calls > SKILL_CANDIDATE_TOOL_CALLS
    )


def _resolve_librarian_pid() -> Optional[int]:
    """Find librarian-pai's pid by scanning proc specs (same shape as the
    `memorize` bin's resolver — librarian is identified by slug)."""
    for slug, spec in P._iter_pai_specs():
        if slug == LIBRARIAN_SLUG:
            pid = spec.get("pid")
            if isinstance(pid, int):
                return pid
    return None


def _maybe_emit_skill_candidate(
    pai_slug: str,
    pai_pid: int,
    duration: float,
    tool_calls: int,
    history_len: int,
    new_history_len: int,
) -> None:
    """Nudge librarian to consider a skill from this turn, if it qualifies.

    Fires the same `pai_message`/`target_pid` event that `memorize` uses, with a
    `[skill-candidate …]` marker pointing librarian at the transcript range it
    should read. Best-effort: never raise into the reply path."""
    try:
        if not _is_skill_candidate(pai_slug, duration, tool_calls):
            return
        lib_pid = _resolve_librarian_pid()
        if lib_pid is None:
            return
        reason = "duration" if duration > SKILL_CANDIDATE_DURATION_SECS else "toolcalls"
        P.emit_event({
            "source": "pai",
            "kind": "pai_message",
            "target_pid": lib_pid,
            "sender_pid": pai_pid,
            "text": (
                f"[skill-candidate from={pai_slug} reason={reason} "
                f"duration={duration:.0f}s tools={tool_calls} "
                f"turns={history_len}..{new_history_len}] "
                f"messages={_history_path_display(pai_slug)}"
            ),
        })
    except Exception as e:
        print(f"[kernel] skill-candidate emit failed (pai={pai_slug}): {e!r}", flush=True)
        try:
            append_log(pai_slug, f"kernel: skill-candidate emit failed — {e!r}")
        except ProcessNotFound:
            pass


async def _nudge_body(
    reason: str,
    slug: Optional[str],
    context: Optional[dict],
    pai_pid: int,
    pai_slug: str,
    from_: Optional[int],
    from_kind: str,
    *,
    status_prefix: Optional[str] = None,
    reply_filter: Optional[Callable[[str], str]] = None,
) -> Optional[str]:
    t0 = time.monotonic()
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

    # First-wake owner profiling. The fallback PAI gets a one-time onboarding
    # instruction appended to its turn until the profile artifact exists.
    # Keying on the artifact (not just the flag) makes retry idempotent: an
    # interrupted/partial pass leaves the flag set and re-injects next wake.
    profile_path = paths_mod.PAI_ROOT / "var/lib/owner/profile.md"
    do_onboarding = (
        bool(pai_spec.get("fallback"))
        and config.onboarding_pending()
        and not profile_path.exists()
    )

    home = stitch.home_for(pai_slug)
    system = bootstrap.build_system_prompt(
        pai=pai_pid,
        parent=parent_pid,
        prompt_dir=pai_spec.get("prompt_dir"),
        prompt_path=pai_spec.get("prompt"),
        boilerplate=pai_spec.get("boilerplate"),
        home_dir=str(home),
        persub=bool(pai_spec.get("persub")),
    )
    sender = f"{from_kind}:{from_}" if from_ is not None else None
    user = bootstrap.build_user_turn(reason, slug, context, sender=sender)
    if do_onboarding:
        user += f"\n\n{_ONBOARDING_INSTRUCTION}"
    if _is_overclock_context(context):
        user += f"\n\n{_overclock_instruction(context)}"

    history_path = _history_path(pai_slug)
    history = _load_history(history_path)

    debugger_cfg = pai_spec.get("debugger") or None
    pre_snapshot: dict[str, float] = {}
    if debugger_cfg:
        try:
            watch_paths = [
                paths_mod.PAI_ROOT / p
                for p in (debugger_cfg.get("watch_paths") or [])
            ]
            excludes = [
                paths_mod.PAI_ROOT / p
                for p in (debugger_cfg.get("exclude") or [])
            ]
            pre_snapshot = debugger.snapshot(watch_paths, excludes)
        except Exception as e:
            try:
                append_log(pai_slug, f"[debugger] pre-snapshot failed — {e!r}")
            except ProcessNotFound:
                pass
            debugger_cfg = None

    env = {
        "PAI_SLUG": pai_slug,
        "PAI_PID": str(pai_pid),
        "PAI_PARENT": parent_str or "",
    }
    # Subagents hand durable artifacts back through the parent's home, which
    # outlives the child's reaped /proc/<slug>/. Expose the path so prompts
    # can name it.
    if parent_pid is not None:
        try:
            parent_home = stitch.home_for(P.find_pai_slug(parent_pid))
            env["PAI_PARENT_HOME"] = str(parent_home)
            env["PAI_RESULT_DIR"] = str((parent_home / "workspace" / pai_slug).resolve(strict=False))
        except ProcessNotFound:
            pass

    def _set_status(reason: str) -> None:
        try:
            if status_prefix:
                P.set_busy_reason(pai_slug, f"{status_prefix} - {reason}")
            else:
                P.set_busy_reason(pai_slug, reason)
        except ProcessNotFound:
            pass

    # Run the turn, with one kernel-side recovery retry if the provider
    # rejects the prompt for exceeding its context window. The retry runs
    # against a freshly reset history, so it fits.
    reply = ""
    new_history: Optional[list[dict]] = None
    for attempt in range(2):
        try:
            reply, new_history = await llm.run_turn(
                system,
                user,
                history=history,
                env=env,
                provider=pai_spec.get("provider"),
                model=pai_spec.get("model"),
                set_status=_set_status,
            )
            break
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
            # Context-window overflow: the soft compaction never took (the
            # model didn't call bin/compact), history grew past the provider's
            # hard limit, and every turn now 400s. Recover kernel-side on the
            # first attempt — archive and reset the conversation so the retry
            # fits — without waiting on the model.
            if attempt == 0 and _is_context_overflow(e):
                archived = _emergency_reset_history(pai_slug, history_path)
                history = []
                note = (
                    f"kernel: context overflow — archived history to "
                    f"{archived} and reset; retrying"
                )
                print(f"[kernel] nudge: pai={pai_slug} {note}", flush=True)
                try:
                    append_log(pai_slug, note)
                except ProcessNotFound:
                    pass
                continue
            if attempt == 0 and _is_transient(e):
                note = f"kernel: transient provider error, retrying once — {e!r}"
                print(f"[kernel] nudge: pai={pai_slug} {note}", flush=True)
                try:
                    append_log(pai_slug, note)
                except ProcessNotFound:
                    pass
                await asyncio.sleep(_TRANSIENT_RETRY_DELAY)
                continue
            await _handle_turn_failure(
                e, reason, slug, pai_pid, pai_slug, is_ephemeral
            )
            return

    if debugger_cfg:
        try:
            await debugger.review(
                pai_slug=pai_slug,
                pai_root=paths_mod.PAI_ROOT,
                config=debugger_cfg,
                history=new_history,
                pre_snapshot=pre_snapshot,
            )
        except Exception as e:
            try:
                append_log(pai_slug, f"[debugger] failed — {e!r}")
            except ProcessNotFound:
                pass

    _save_history(history_path, new_history)

    # Onboarding completes when the profile artifact exists. Clearing keyed on
    # the produced file (not merely "we ran the turn") gives idempotent retry:
    # a partial pass leaves the flag set and re-injects next wake. Config
    # mutation stays kernel-side — no PAI-called privileged bin.
    if do_onboarding and profile_path.exists():
        try:
            config.clear_onboarding_pending()
            append_log(pai_slug, "kernel: onboarding complete — cleared onboarding_pending")
            # The profiling pass leaves ~30K of exploration in the live buffer.
            # The durable distillation is in var/lib/owner/profile.md (re-injected
            # into every system prompt), so the raw exploration is pure scaffolding
            # — archive and empty it so steady-state turns start lean.
            archived = _clear_history(pai_slug, history_path, "onboarding")
            if archived:
                append_log(pai_slug, f"kernel: onboarding context cleared — archived to "
                                     f"{archived.relative_to(PROC_DIR.parent)}")
        except ProcessNotFound:
            pass
        except Exception as e:
            print(f"[kernel] clear_onboarding_pending failed: {e!r}", flush=True)

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
        "messages_path": _history_path_display(pai_slug),
    })

    # Procedural self-learning: after a non-trivial turn, nudge librarian to
    # consider distilling the workflow into a SKILL.md. The `!= librarian-pai`
    # loop guard (in `_is_skill_candidate`) keeps librarian's own turns — which
    # hit this same path — from re-waking it. Best-effort; never raises.
    _maybe_emit_skill_candidate(
        pai_slug,
        pai_pid,
        time.monotonic() - t0,
        _count_tool_calls(new_history[len(history):]),
        len(history),
        len(new_history),
    )

    # Append-only turn audit log. Consumed by the macOS app's
    # NotifyWatcher to post "PAI <slug> finished" notifications.
    # Wrapped so a disk-full / permission error here cannot break the
    # PAI's reply path.
    try:
        turns_log = paths_mod.var_log() / "turns.jsonl"
        turns_log.parent.mkdir(parents=True, exist_ok=True)
        with turns_log.open("a") as f:
            f.write(json.dumps({
                "ts": datetime.now().isoformat(timespec="seconds"),
                "slug": pai_slug,
                "turn_index": len(new_history),
            }) + "\n")
    except OSError as e:
        print(f"[kernel] turns.jsonl append failed: {e!r}", flush=True)

    _apply_history_action(pai_slug, history_path)

    # The child may have ended its turn by resolving its own proc (the standard
    # `bin/subagent done` exit) or been killed by its parent mid-turn. In either
    # case the kernel's post-turn exit below would clash with that — duplicate
    # response + a failing redundant resolve — so detect it once and stand down.
    already_resolved = _proc_already_resolved(pai_slug)

    auto_finished = False
    if reply:
        visible_reply = reply_filter(reply) if reply_filter else reply
        if visible_reply:
            print(f"[pai:{pai_slug}] {visible_reply}", flush=True)
        if (
            visible_reply
            and parent_pid is not None
            and not already_resolved
            and _is_ad_hoc_subagent(pai_spec)
        ):
            auto_finished = _auto_finish_subagent_plain_reply(
                pai_slug=pai_slug,
                pai_pid=pai_pid,
                parent_pid=parent_pid,
                visible_reply=visible_reply,
            )
        # Top-level fleet PAIs (no parent) write back to the owner's
        # me-thread so the TUI chat tab shows their replies. Persubs
        # are also owner-addressable (the user opens a tab for them
        # in the TUI), so their replies belong in the me-thread too.
        # Plain ephemeral subagents talk to their parent via
        # subagent:response, not the me-thread, so they're excluded.
        elif visible_reply and (not parent_str or pai_spec.get("persub")):
            _append_to_me_thread(pai_pid, visible_reply)
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

    if is_ephemeral and not auto_finished and not already_resolved:
        try:
            P.resolve(pai_slug, "completed")
        except ProcessNotFound:
            pass
    return reply
