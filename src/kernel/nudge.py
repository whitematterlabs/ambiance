"""Nudge — the single entrypoint for waking PAI.

Assembles the bootstrap (system prompt + user turn) and runs one LLM
turn against the filesystem. Loads the target PAI's prior conversation
history from proc/<pai>/messages.jsonl, threads it through the turn,
and persists the updated history back on completion.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from . import bootstrap, llm
from . import processes as P
from .processes import LIVE_DIR, ProcessNotFound, append_log


def _history_path(pai: str) -> Path:
    return LIVE_DIR / "proc" / str(pai) / "messages.jsonl"


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


def _append_to_me_thread(pai: str, text: str) -> None:
    """Post PAI's reply to today's me/<pai>/<date>.md as `[HH:MM] pai: ...`."""
    day = date.today().isoformat()
    path = LIVE_DIR / "communication" / "messages" / "me" / str(pai) / f"{day}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    hm = datetime.now().strftime("%H:%M")
    # Collapse internal newlines — one message = one line.
    flat = " ".join(text.splitlines()).strip()
    if not flat:
        return
    with path.open("a", encoding="utf-8") as f:
        f.write(f"[{hm}] pai: {flat}\n")


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


def _apply_history_action(pai_slug: str, history_path: Path) -> bool:
    """If PAI queued a clear/compact via `bin/clear` or `bin/compact` during
    the turn, apply it now: archive the just-saved history and rewrite the
    live jsonl. Returns True if an action was applied."""
    proc_dir = LIVE_DIR / "proc" / pai_slug
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

    rel_archive = archive_path.relative_to(LIVE_DIR)

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
    pai: str = "1",
) -> None:
    header = f"[kernel] nudge: {reason}"
    if slug:
        header += f" ({slug})"
    print(header, flush=True)

    pai_slug = str(pai)
    log_line = f"nudge: {reason}" + (f" ({slug})" if slug else "")
    try:
        append_log(pai_slug, log_line)
    except ProcessNotFound:
        pass

    if slug and slug != pai_slug:
        try:
            append_log(slug, f"kernel: nudge — {reason}")
        except ProcessNotFound:
            pass

    try:
        pai_spec = P.read_spec(pai_slug)
    except ProcessNotFound:
        pai_spec = {}
    parent = pai_spec.get("parent")
    parent_str = str(parent) if parent is not None else None

    system = bootstrap.build_system_prompt(pai=pai_slug, parent=parent_str)
    user = bootstrap.build_user_turn(reason, slug, context)

    history_path = _history_path(pai_slug)
    history = _load_history(history_path)

    env = {"PAI_SLUG": pai_slug, "PAI_PARENT": parent_str or ""}

    try:
        reply, new_history = await llm.run_turn(system, user, history=history, env=env)
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
        if parent_str:
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
        if parent_str:
            try:
                P.resolve(pai_slug, "failed")
            except ProcessNotFound:
                pass
        return

    _save_history(history_path, new_history)
    _apply_history_action(pai_slug, history_path)

    if reply:
        print(f"[pai:{pai_slug}] {reply}", flush=True)
        if not parent_str:
            _append_to_me_thread(pai_slug, reply)
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

    if parent_str:
        try:
            P.resolve(pai_slug, "completed")
        except ProcessNotFound:
            pass
