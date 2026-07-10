"""Claude Code turn-executor backend.

A drop-in alternative to :func:`llm.run_turn` that executes one PAI turn by
driving the ``claude`` CLI (Claude Code) inside the PAI's real FHS home,
rather than looping over the Anthropic Messages API in-process.

Contract (identical to ``llm.run_turn``)::

    run_turn(system, user, history, env, *, provider, model, set_status)
        -> (final_text, messages)

Selected per-PAI via ``backend: claudecode`` in ``etc/config.yaml``. Only the
"think one turn" verb changes — the kernel's event loop, drivers, fleet, nudge
model, injection plumbing, and history persistence are all untouched, because
they never touch the model (the sole product seam is this one call).

How it maps onto PAI:

* **Persona** — the fully-assembled PAI system prompt is handed to
  ``--system-prompt`` (a full *replacement* of Claude Code's own identity), so
  the PAI is still the PAI, not Claude Code wearing a hat.
* **Context** — ``cwd`` and ``HOME`` are the PAI's real FHS home, so Claude
  Code operates natively inside the PAI's world (its skills, memory, inbox,
  binaries on PATH). The FHS *is* the context.
* **Continuity** — Claude Code keeps its own session transcript; we pin a
  stable session id per PAI (``/proc/<slug>/claude-session``) and ``--resume``
  it each turn. PAI's ``messages.jsonl`` gets a bridged record (user + final
  text) so compaction / me-thread / accounting keep working.

Auth: ``claude`` normally reads a Keychain OAuth token resolved via ``HOME``,
which the kernel repoints to the PAI's home — so the Keychain lookup fails. We
inject a HOME-independent credential instead: ``CLAUDE_CODE_OAUTH_TOKEN``
(subscription, from ``claude setup-token``) or ``ANTHROPIC_API_KEY`` (API
billing). Provision it once via ``etc/claudecode-token`` or the kernel env.
Without one the turn fails closed with an instruction.

Not yet ported (phase 2): mid-turn injection (needs the Agent SDK's streaming
input — the one-shot CLI has no tool-boundary to inject at) and live per-tool
status. Both degrade gracefully here: injected messages re-queue for the next
turn, and status shows a single "thinking" line.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import uuid
from pathlib import Path
from typing import Optional

from . import llm, tokens
from .paths import PAI_ROOT
from .processes import PROC_DIR

# Owner-provisioned, HOME-independent credential. Checked before the kernel
# env so a dropped file wins without a kernel restart.
_TOKEN_FILE = PAI_ROOT / "etc" / "claudecode-token"

_SETUP_HINT = (
    "claudecode backend has no credential. Run `claude setup-token` (needs a "
    "Claude subscription) and write the token to "
    f"{_TOKEN_FILE} — or set CLAUDE_CODE_OAUTH_TOKEN / ANTHROPIC_API_KEY in the "
    "kernel env. Auth must be HOME-independent because the PAI's HOME is "
    "repointed at its FHS home, which breaks the Keychain lookup."
)


class ClaudeBackendError(RuntimeError):
    """A claude invocation failed in a way the kernel should log + surface."""


def _claude_bin() -> str:
    exe = shutil.which("claude")
    if not exe:
        raise ClaudeBackendError("`claude` not found on PATH")
    return exe


def _auth_env() -> dict[str, str]:
    """The one credential var to inject, or {} if none is provisioned.

    Subscription token wins over an API key when both are present."""
    tok = None
    try:
        tok = _TOKEN_FILE.read_text().strip() or None
    except OSError:
        tok = None
    tok = tok or os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    if tok:
        return {"CLAUDE_CODE_OAUTH_TOKEN": tok}
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return {"ANTHROPIC_API_KEY": key}
    return {}


def _session_file(slug: str) -> Path:
    return PROC_DIR / slug / "claude-session"


def _read_session(slug: str) -> Optional[str]:
    try:
        return _session_file(slug).read_text().strip() or None
    except OSError:
        return None


def _write_session(slug: str, sid: str) -> None:
    try:
        p = _session_file(slug)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(sid)
        os.replace(tmp, p)
    except OSError:
        pass


def clear_session(slug: str) -> None:
    """Forget the PAI's claude session so the next turn starts fresh.

    Call this wherever PAI's own history is reset (compaction / emergency
    overflow) so the two transcripts don't drift."""
    try:
        _session_file(slug).unlink()
    except OSError:
        pass


def _child_env(env: Optional[dict]) -> dict[str, str]:
    """Env for the claude subprocess: kernel env + PAI env, minus any inherited
    Claude Code markers (the kernel may itself be launched from a claude
    session during dev), plus the injected credential."""
    child = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("CLAUDE") and k != "CLAUDECODE"
    }
    if env:
        child.update({k: str(v) for k, v in env.items()})
    child.update(_auth_env())
    return child


def _build_args(model: Optional[str], system: str, sid: str, resume: bool) -> list[str]:
    args = [
        "-p",
        "--system-prompt",
        system,
        "--output-format",
        "json",
        "--permission-mode",
        "bypassPermissions",
    ]
    if model:
        args += ["--model", model]
    args += (["--resume", sid] if resume else ["--session-id", sid])
    return args


async def _invoke(
    exe: str, args: list[str], user: str, cwd: Path, child_env: dict[str, str]
) -> dict:
    """Run one claude subprocess, feeding `user` on stdin, return parsed JSON.

    Raises ClaudeBackendError on spawn/parse failure. On asyncio cancellation,
    kills the process group and re-raises CancelledError."""
    proc = await asyncio.create_subprocess_exec(
        exe,
        *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(cwd),
        env=child_env,
        start_new_session=True,
    )
    try:
        out, err = await proc.communicate(user.encode())
    except asyncio.CancelledError:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass
        raise
    if not out.strip():
        tail = err.decode(errors="replace").strip()[-500:]
        raise ClaudeBackendError(f"claude produced no output (stderr: {tail!r})")
    try:
        return json.loads(out.decode())
    except json.JSONDecodeError as e:
        raise ClaudeBackendError(f"claude output was not JSON: {e} — {out[:500]!r}")


async def run_turn(
    system: str,
    user: str,
    history: Optional[list[dict]] = None,
    env: Optional[dict] = None,
    *,
    provider: Optional[str] = None,  # unused; claude owns its own routing
    model: Optional[str] = None,
    set_status: Optional[callable] = None,
) -> tuple[str, list[dict]]:
    """Execute one PAI turn via the ``claude`` CLI. See module docstring.

    Returns ``(final_text, messages)`` where ``messages`` = history + the user
    turn + the assistant's final text, in the same on-disk block shape
    ``nudge._save_history`` expects.

    On cancellation raises :class:`llm.TurnCancelled` with the pruned history
    (history + user turn), matching the Anthropic backend's contract so the
    kernel's existing handler works unchanged.
    """
    slug = (env or {}).get("PAI_SLUG") or "?"
    home = Path((env or {}).get("HOME") or PAI_ROOT)
    history = list(history) if history else []
    base_messages = history + [{"role": "user", "content": user}]

    if not _auth_env():
        raise ClaudeBackendError(_SETUP_HINT)

    exe = _claude_bin()
    child_env = _child_env(env)

    def _status(reason: str) -> None:
        if set_status:
            try:
                set_status(reason)
            except Exception:
                pass

    _status("thinking (claude code)")

    prior = _read_session(slug)
    sid = prior or str(uuid.uuid4())
    resume = prior is not None

    try:
        try:
            data = await _invoke(exe, _build_args(model, system, sid, resume), user, home, child_env)
        except asyncio.CancelledError:
            llm._prune_unresolved_tool_uses(base_messages)
            raise llm.TurnCancelled(base_messages)

        # A resume against a session claude no longer knows about fails; start
        # a fresh session once so a wiped/rotated transcript self-heals.
        if resume and data.get("is_error") and _looks_like_missing_session(data):
            clear_session(slug)
            sid = str(uuid.uuid4())
            try:
                data = await _invoke(exe, _build_args(model, system, sid, False), user, home, child_env)
            except asyncio.CancelledError:
                llm._prune_unresolved_tool_uses(base_messages)
                raise llm.TurnCancelled(base_messages)
    except llm.TurnCancelled:
        raise
    except ClaudeBackendError:
        raise

    result = (data.get("result") or "").strip()
    if data.get("is_error"):
        raise ClaudeBackendError(f"claude turn failed: {result or data.get('subtype')!r}")

    # Pin whatever session id claude actually used (it may fork/rotate) so the
    # next turn resumes the right transcript.
    got_sid = data.get("session_id")
    if got_sid:
        _write_session(slug, got_sid)

    usage = data.get("usage")
    if usage:
        tokens.record(slug, data.get("model") or model or "claude", usage)

    messages = base_messages + [{"role": "assistant", "content": result}]
    return result, messages


def _looks_like_missing_session(data: dict) -> bool:
    txt = f"{data.get('result') or ''} {data.get('subtype') or ''}".lower()
    return "no conversation found" in txt or "session" in txt and "not found" in txt
