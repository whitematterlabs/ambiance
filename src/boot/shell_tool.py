"""The single tool exposed to PAI: a bash shell rooted at home/.

Backed by a per-PAI tmux session so:
  - state (cwd, env, history) persists across tool calls,
  - interactive CLIs and TUIs get a real PTY,
  - the owner can `tmux attach -t pai-<slug>` to watch live.

Two modes via the same tool:
  - `command`: run a bash command to completion. Polls the pane until
    a per-call sentinel marker appears, then returns the slice between
    start/end markers and the parsed exit code. Default mode.
  - `keys`: send raw keystrokes to whatever is currently running in the
    foreground (interactive prompts, vim, claude, etc.). Returns a
    snapshot of the rendered screen plus cursor position. No exit code.

Freesolo by design. The agent is trusted in its own world.
"""

from __future__ import annotations

import asyncio
import os
import re
import secrets
from dataclasses import dataclass
from typing import Optional

from . import stitch
from .paths import PAI_ROOT
from .processes import HOME_DIR

TOOL_NAME = "bash"
TOOL_DESCRIPTION = (
    "Run bash in PAI's persistent shell. State (cwd, env, history) "
    "survives across calls — this is one long-lived shell, not a fresh "
    "subprocess per call. PAI's filesystem is rooted at an FHS layout — "
    "`/etc/`, `/usr/`, `/var/`, `/proc/`, `/run/`, `/sys/`, `/boot/`, "
    "`/sbin/`, `/bin/`, `/opt/`, `/home/`, `/root/`, `/tmp/` all refer "
    "to PAI's world; FHS prefixes are rewritten to PAI's root before "
    "exec.\n\n"
    "Two modes (exactly one of `command` or `keys` must be set):\n"
    "  - `command` — run a bash command to completion; returns combined "
    "stdout+stderr and exit code. Use this by default.\n"
    "  - `keys` — send raw keystrokes to whatever is currently running "
    "(interactive prompts, vim, htop, claude). Returns the visible "
    "screen and cursor position. Use this only when something is "
    "already running in the foreground and waiting for input. To "
    "interrupt a stuck command, send `keys: \"C-c\"`."
)


# FHS slot prefixes that should be rewritten to live under PAI_ROOT.
# /dev and /mnt are intentionally NOT rewritten — /dev/null, /dev/stdin
# etc. are real OS facilities PAI legitimately uses.
_FHS_SLOTS = (
    "etc", "usr", "var", "proc", "run", "sys",
    "boot", "sbin", "bin", "opt", "home", "root", "tmp",
)
_FHS_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_./])"                    # not mid-token / not a hostname
    r"(/(?:" + "|".join(_FHS_SLOTS) + r"))"   # the FHS prefix
    r"(?=/|\s|$|[\"'`\);\],:|&>])"            # boundary: path continues, or shell delimiter
)


def rewrite_fhs_paths(command: str, root: str) -> str:
    """Rewrite leading-slash FHS paths to live under `root`.

    Substring substitution with anchored boundaries — robust across
    quoting, heredocs, and `bash -c` / `python -c` strings, since we
    operate on the raw command before bash tokenizes anything.
    """
    return _FHS_PATTERN.sub(lambda m: f"{root}{m.group(1)}", command)


TOOL_SCHEMA = {
    "name": TOOL_NAME,
    "description": TOOL_DESCRIPTION,
    "input_schema": {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": (
                    "Bash command to run to completion in the persistent "
                    "shell. State (cwd, env, history) carries across "
                    "calls."
                ),
            },
            "keys": {
                "type": "string",
                "description": (
                    "Raw keystrokes to send to the currently-running "
                    "foreground program. Whitespace-separated tokens; "
                    "tmux key names like Enter, Tab, Escape, Space, "
                    "BSpace, Up/Down/Left/Right, PageUp/PageDown, "
                    "Home, End, C-<x> (Ctrl-x), M-<x> (Meta-x), "
                    "F1..F12 are sent as keys; other tokens are typed "
                    "as literal text. To type a literal space use the "
                    "`Space` token (e.g. `hello Space world Enter`)."
                ),
            },
            "wait_ms": {
                "type": "integer",
                "description": (
                    "Used only with `keys`: ms to pause after sending "
                    "before snapshotting the screen. Default 300."
                ),
            },
        },
    },
}


@dataclass
class ShellResult:
    stdout: str
    stderr: str
    exit_code: Optional[int]

    def render(self) -> str:
        out = self.stdout.rstrip("\n")
        err = self.stderr.rstrip("\n")
        parts = []
        if out:
            parts.append(out)
        if err:
            parts.append(f"[stderr]\n{err}")
        if self.exit_code is not None:
            parts.append(f"[exit {self.exit_code}]")
        return "\n".join(parts)


# ---------------------------------------------------------------------------
# tmux backend


def _socket_path(slug: str) -> str:
    run_dir = PAI_ROOT / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    return str(run_dir / f"tmux-{slug}.sock")


def _target(slug: str) -> str:
    return f"pai-{slug}"


async def _tmux(
    sock: str,
    *args: str,
    env: Optional[dict] = None,
) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        "tmux", "-S", sock, *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env if env is not None else os.environ.copy(),
    )
    out, err = await proc.communicate()
    return proc.returncode if proc.returncode is not None else -1, out, err


async def _has_session(sock: str, slug: str) -> bool:
    rc, _, _ = await _tmux(sock, "has-session", "-t", _target(slug))
    return rc == 0


async def _kill_session(sock: str, slug: str) -> None:
    await _tmux(sock, "kill-session", "-t", _target(slug))


async def _ensure_session(slug: str, cwd: str, env: dict) -> None:
    sock = _socket_path(slug)
    if await _has_session(sock, slug):
        return
    rc, _, err = await _tmux(
        sock, "new-session", "-d",
        "-s", _target(slug),
        "-x", "120", "-y", "40",
        "-c", cwd,
        env=env,
    )
    if rc != 0:
        raise RuntimeError(
            f"tmux new-session failed for {slug}: {err.decode('utf-8', 'replace')}"
        )
    # Set a deterministic prompt and clear startup noise. The shell may
    # need a beat to be ready to accept keystrokes.
    await asyncio.sleep(0.1)
    await _tmux(
        sock, "send-keys", "-t", _target(slug),
        "export PS1='$ '; clear", "Enter",
    )
    await asyncio.sleep(0.1)


async def _capture(sock: str, slug: str, full: bool) -> str:
    args = ["capture-pane", "-p", "-t", _target(slug)]
    if full:
        args[1:1] = ["-S", "-100000"]
    rc, out, _ = await _tmux(sock, *args)
    if rc != 0:
        return ""
    return out.decode("utf-8", errors="replace")


async def _exec_command(slug: str, command: str, cwd: str, env: dict) -> ShellResult:
    sock = _socket_path(slug)
    await _ensure_session(slug, cwd, env)
    rewritten = rewrite_fhs_paths(command, str(PAI_ROOT))
    nonce = secrets.token_hex(8)
    start_marker = f"__PAI_START_{nonce}"
    # Subshell so the inner command's exit status is captured cleanly,
    # but cd / export still affect the parent shell when not in a subshell —
    # so we DON'T wrap in (...). Use { ...; } group instead, which keeps
    # state changes in the persistent shell while still letting `$?` reach
    # our trailing echo.
    built = (
        f"echo {start_marker}; "
        f"{{ {rewritten}; }}; "
        f"echo __PAI_DONE_{nonce}_$?"
    )
    end_re = re.compile(rf"^__PAI_DONE_{nonce}_(\d+)\s*$", re.M)
    start_re = re.compile(rf"^{start_marker}\s*$", re.M)

    async def _send() -> tuple[int, bytes]:
        rc, _, err = await _tmux(
            sock, "send-keys", "-t", _target(slug), built, "Enter"
        )
        return rc, err

    rc, err = await _send()
    if rc != 0:
        # Session may have died mid-call — recreate once and retry.
        await _kill_session(sock, slug)
        try:
            await _ensure_session(slug, cwd, env)
        except RuntimeError as exc:
            return ShellResult(stdout="", stderr=str(exc), exit_code=-1)
        rc, err = await _send()
        if rc != 0:
            return ShellResult(
                stdout="",
                stderr=err.decode("utf-8", "replace"),
                exit_code=-1,
            )

    while True:
        captured = await _capture(sock, slug, full=True)
        m_end = end_re.search(captured)
        if m_end:
            exit_code = int(m_end.group(1))
            m_start = start_re.search(captured)
            if m_start and m_start.end() <= m_end.start():
                output = captured[m_start.end():m_end.start()]
            else:
                output = captured[:m_end.start()]
            output = output.lstrip("\n").rstrip()
            return ShellResult(stdout=output, stderr="", exit_code=exit_code)
        await asyncio.sleep(0.05)


def _parse_keys(keys: str) -> list[str]:
    """Whitespace-tokenize a keys string for tmux send-keys.

    tmux send-keys recognizes named keys (Enter, Tab, C-c, etc.) and
    treats anything else as literal text. We just split on whitespace
    and pass the tokens through; to type literal whitespace, callers
    use the `Space` token.
    """
    return keys.split()


async def _send_keys_mode(
    slug: str, keys: str, wait_ms: int, cwd: str, env: dict
) -> ShellResult:
    sock = _socket_path(slug)
    await _ensure_session(slug, cwd, env)
    tokens = _parse_keys(keys)
    if not tokens:
        return ShellResult(stdout="", stderr="empty keys", exit_code=-1)
    rc, _, err = await _tmux(sock, "send-keys", "-t", _target(slug), *tokens)
    if rc != 0:
        await _kill_session(sock, slug)
        try:
            await _ensure_session(slug, cwd, env)
        except RuntimeError as exc:
            return ShellResult(stdout="", stderr=str(exc), exit_code=-1)
        rc, _, err = await _tmux(sock, "send-keys", "-t", _target(slug), *tokens)
        if rc != 0:
            return ShellResult(
                stdout="", stderr=err.decode("utf-8", "replace"), exit_code=-1
            )
    await asyncio.sleep(max(0, wait_ms) / 1000)
    screen = await _capture(sock, slug, full=False)
    rc, cur_out, _ = await _tmux(
        sock, "display-message", "-p", "-t", _target(slug),
        "#{cursor_x},#{cursor_y}",
    )
    cursor = cur_out.decode("utf-8", "replace").strip() if rc == 0 else ""
    body = screen.rstrip("\n")
    if cursor:
        body = f"{body}\n[cursor {cursor}]"
    return ShellResult(stdout=body, stderr="", exit_code=None)


# ---------------------------------------------------------------------------
# Public dispatcher


async def run(
    tool_input: dict | str,
    env: Optional[dict] = None,
) -> ShellResult:
    # Accept a bare command string for backwards compat with internal callers.
    if isinstance(tool_input, str):
        tool_input = {"command": tool_input}

    command = tool_input.get("command")
    keys = tool_input.get("keys")
    wait_ms_raw = tool_input.get("wait_ms")
    try:
        wait_ms = int(wait_ms_raw) if wait_ms_raw is not None else 300
    except (TypeError, ValueError):
        wait_ms = 300

    if command and keys:
        return ShellResult(
            stdout="",
            stderr="bash tool: pass `command` OR `keys`, not both",
            exit_code=-1,
        )
    if not command and not keys:
        return ShellResult(
            stdout="",
            stderr="bash tool: must pass `command` or `keys`",
            exit_code=-1,
        )

    # Per-PAI cwd: each PAI's stitched home (root → /root/, others → /home/<slug>/).
    # Falls back to the global HOME_DIR if the caller didn't pass PAI_SLUG.
    raw_slug = (env or {}).get("PAI_SLUG")
    slug = raw_slug or "pai"
    cwd = stitch.home_for(raw_slug) if raw_slug else HOME_DIR
    cwd.mkdir(parents=True, exist_ok=True)

    # Prepend the kernel venv + PAI bin slots so tool shebangs like
    # `#!/usr/bin/env python` resolve to the venv interpreter (which has
    # the deps the bins import) and bare bin names work without paths.
    pai_path_prefix = os.pathsep.join([
        str(PAI_ROOT / "usr" / "lib" / "venv" / "bin"),
        str(PAI_ROOT / "usr" / "bin"),
        str(PAI_ROOT / "sbin"),
    ])
    base_env = {**os.environ}
    base_env["PATH"] = pai_path_prefix + os.pathsep + base_env.get("PATH", "")
    base_env["TERM"] = "xterm-256color"
    proc_env = {**base_env, **env} if env else base_env

    if keys:
        return await _send_keys_mode(slug, keys, wait_ms, str(cwd), proc_env)
    return await _exec_command(slug, command, str(cwd), proc_env)
