"""Persistent PTY-backed bash session — the `shell` tool.

Backed by a kernel-owned bash subprocess running under a PTY so:
  - state (cwd, env, history) persists across tool calls,
  - interactive CLIs and TUIs (vim, claude, npm init) get a real PTY,
  - the owner can `tmux -S run/tmux-<slug>.sock attach -t pai-<slug>`
    to watch a live tail of decoded commands and outputs.

Two modes via the same tool:
  - `command`: run a bash command to completion. The command is
    base64-encoded and `eval`'d inside a helper function whose stdout
    and stderr are redirected to dedicated side-channel pipes; the
    exit code arrives on a third pipe. The kernel's writer-side parser
    interaction is a single fixed-shape line (`_pai_run <b64>\\n`),
    which has no quoting surface — structurally impossible to wedge
    bash into a continuation prompt.
  - `keys`: send raw keystrokes to whatever is currently running in
    the foreground (vim, htop, claude, prompts). The PTY master is
    fed into an in-process pyte virtual terminal; snapshots return
    the rendered screen + cursor position.

For one-shot stateless commands (the 95% case), use the sibling `bash`
tool instead — a clean subprocess, no PTY, no persistence. Reach for
`shell` only when you need persistent cwd/env, a TUI, or to send
keystrokes to a running foreground program.

Freesolo by design. The agent is trusted in its own world.
"""

from __future__ import annotations

import asyncio
import base64
import fcntl
import os
import pty
import struct
import subprocess
import termios
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import pyte

from . import stitch
from ._shell_common import (
    ShellResult,
    fhs_reject_message,
    find_fhs_spellings,
    log_fhs_reject,
)
from .paths import PAI_ROOT, build_pai_path
from .processes import HOME_DIR

TOOL_NAME = "shell"
TOOL_DESCRIPTION = (
    "Persistent PTY-backed bash session. Use this when you need "
    "persistent cwd/env across calls, an interactive TUI (vim, htop, "
    "claude), or to send keystrokes to a foreground program. "
    "For one-shot stateless commands, prefer the `bash` tool — it "
    "is faster, isolated, and avoids PTY-inherited termios surprises.\n\n"
    "PAI's filesystem is rooted at an FHS layout — `/etc/`, `/usr/`, "
    "`/var/`, `/proc/`, `/run/`, `/sys/`, `/boot/`, `/sbin/`, `/bin/`, "
    "`/opt/`, `/home/`, `/root/`, `/tmp/` all refer to PAI's world; "
    "FHS prefixes are rewritten to PAI's root before exec. Bare Unix "
    "commands resolve to host macOS binaries first; invoke PAI tools as "
    "`bin/<name>` when names collide (`bin/ps`, `bin/cal`, `bin/clear`).\n\n"
    "Two modes (exactly one of `command` or `keys` must be set):\n"
    "  - `command` — run a bash command to completion in the persistent "
    "shell; returns combined stdout+stderr and exit code.\n"
    "  - `keys` — send raw keystrokes to whatever is currently running "
    "(interactive prompts, vim, htop, claude). Returns the visible "
    "screen and cursor position. Use this only when something is "
    "already running in the foreground and waiting for input. To "
    "interrupt a stuck command, send `keys: \"C-c\"`.\n\n"
    "Timeouts. `command` mode has a wall-clock cap via `timeout_ms` "
    "(default 120000, max 600000). On expiry the foreground command "
    "gets SIGINT, partial stdout/stderr come back with exit_code -1, "
    "and you can react. Raise it for slow work — test runs, builds, "
    "slow CLIs, AppleScript queries. The cap is the cap; don't fight "
    "it, background instead.\n\n"
    "Background work. For anything that legitimately needs to outlive "
    "a single tool call (long batch jobs, watchers, servers, slow "
    "queries), background it: "
    "`nohup cmd > /tmp/<descriptive-name>.log 2>&1 & echo $!`. "
    "Capture the PID and remember the log path. Manage across "
    "subsequent calls with `tail /tmp/<name>.log`, `ps -p $pid`, "
    "`kill $pid`, `wait $pid`. The shell session is persistent across "
    "tool calls for this PAI's lifetime, so PIDs and `jobs` survive "
    "between invocations."
)


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
                    "named keys Enter, Tab, Escape, Space, BSpace, "
                    "Up/Down/Left/Right, PageUp/PageDown, Home, End, "
                    "C-<x> (Ctrl-x), M-<x> (Meta-x), F1..F12 are sent "
                    "as keys; other tokens are typed as literal text. "
                    "To type a literal space use the `Space` token "
                    "(e.g. `hello Space world Enter`)."
                ),
            },
            "wait_ms": {
                "type": "integer",
                "description": (
                    "Used only with `keys`: ms to pause after sending "
                    "before snapshotting the screen. Default 300."
                ),
            },
            "timeout_ms": {
                "type": "integer",
                "description": (
                    "Wall-clock timeout in milliseconds for `command` mode. "
                    "Default 120000 (2 min). Maximum 600000 (10 min). "
                    "On timeout the foreground command is sent SIGINT, partial "
                    "stdout/stderr are returned, and exit_code is -1. For work "
                    "that must run longer than 10 min, background it with "
                    "`nohup ... &` (see tool description). Ignored in `keys` mode."
                ),
            },
        },
    },
}


# ---------------------------------------------------------------------------
# PTY + side-channel backend


_ROWS = 40
_COLS = 120
_DEFAULT_TIMEOUT_MS = 120_000  # 2 min default per command
_MAX_TIMEOUT_MS = 600_000  # 10 min cap; background longer work via nohup


@dataclass
class _Session:
    slug: str
    proc: subprocess.Popen
    master_fd: int
    exit_r: int
    out_r: int
    err_r: int
    screen: pyte.Screen
    stream: pyte.ByteStream
    live_log: object  # binary file
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    stdout_buf: bytearray = field(default_factory=bytearray)
    stderr_buf: bytearray = field(default_factory=bytearray)
    exit_buf: bytearray = field(default_factory=bytearray)
    exit_event: asyncio.Event = field(default_factory=asyncio.Event)


_sessions: dict[str, _Session] = {}
_spawn_lock = asyncio.Lock()


def _socket_path(slug: str) -> Path:
    run_dir = PAI_ROOT / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir / f"tmux-{slug}.sock"


def _live_log_path(slug: str) -> Path:
    p = PAI_ROOT / "var" / "log" / "pai"
    p.mkdir(parents=True, exist_ok=True)
    return p / f"{slug}.live"


def _set_winsize(fd: int, rows: int, cols: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def _set_nonblocking(fd: int) -> None:
    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)


def _setup_slave_termios(fd: int) -> None:
    # Clear ECHO so the PTY doesn't echo our `_pai_run <b64>` line back into
    # the master, and clear ICANON so the line discipline stops buffering by
    # line. With ICANON on, macOS caps a canonical input line at MAX_CANON
    # (1024 bytes) and silently drops the rest — long commands (heredocs,
    # large printfs) get truncated past byte 1024, the `_pai_run` payload
    # arrives malformed, and the eval fails silently. Leave ISIG on so a
    # `\x03` written to the master still raises SIGINT in the foreground
    # pgroup (the timeout / interrupt paths depend on this).
    attrs = termios.tcgetattr(fd)
    # lflag is index 3
    attrs[3] &= ~(termios.ECHO | termios.ICANON)
    termios.tcsetattr(fd, termios.TCSANOW, attrs)


def _safe_write_log(sess: _Session, data: bytes) -> None:
    try:
        sess.live_log.write(data)
    except Exception:
        pass


def _on_master_readable(sess: _Session) -> None:
    try:
        data = os.read(sess.master_fd, 65536)
    except (BlockingIOError, InterruptedError):
        return
    except OSError:
        return
    if not data:
        return
    try:
        sess.stream.feed(data)
    except Exception:
        pass
    _safe_write_log(sess, data)


def _on_out_readable(sess: _Session) -> None:
    try:
        data = os.read(sess.out_r, 65536)
    except (BlockingIOError, InterruptedError):
        return
    except OSError:
        return
    if not data:
        return
    sess.stdout_buf.extend(data)
    _safe_write_log(sess, data)


def _on_err_readable(sess: _Session) -> None:
    try:
        data = os.read(sess.err_r, 65536)
    except (BlockingIOError, InterruptedError):
        return
    except OSError:
        return
    if not data:
        return
    sess.stderr_buf.extend(data)
    _safe_write_log(sess, b"[stderr] ")
    _safe_write_log(sess, data)


def _on_exit_readable(sess: _Session) -> None:
    try:
        data = os.read(sess.exit_r, 4096)
    except (BlockingIOError, InterruptedError):
        return
    except OSError:
        return
    if not data:
        return
    sess.exit_buf.extend(data)
    if b"\n" in sess.exit_buf:
        sess.exit_event.set()


_INIT_SCRIPT = (
    # Job control on, even though bash isn't interactive — this puts each
    # foreground command in its own pgroup, so writing \x03 to the PTY
    # only kills the foreground command, not the persistent bash itself.
    "set -m\n"
    'export PS1="$ "\n'
    "_pai_run() { "
    'eval "$(printf %s "$1" | base64 -d)" >&"$PAI_FD_OUT" 2>&"$PAI_FD_ERR"; '
    'printf "%d\\n" "$?" >&"$PAI_FD_EXIT"; '
    "}\n"
)


async def _write_all_to_master(fd: int, data: bytes) -> None:
    # PTY master is non-blocking; a single os.write can short-write when
    # the slave's input buffer fills (~4KB on macOS). Loop until everything
    # lands, yielding to the event loop on EAGAIN so pipe readers drain.
    n = 0
    while n < len(data):
        try:
            n += os.write(fd, data[n:])
        except BlockingIOError:
            await asyncio.sleep(0.005)


async def _drain_pipe_pending(sess: _Session) -> None:
    """Give the event loop a couple of ticks to drain pipe readers
    after exit_event fires, so trailing fd4/fd5 bytes land in the
    per-call buffers before we snapshot them.
    """
    for _ in range(4):
        await asyncio.sleep(0.005)


async def _exec_via_session(
    sess: _Session, command: str, timeout: float = _DEFAULT_TIMEOUT_MS / 1000
) -> ShellResult:
    async with sess.lock:
        sess.stdout_buf.clear()
        sess.stderr_buf.clear()
        sess.exit_buf.clear()
        sess.exit_event.clear()

        b64 = base64.b64encode(command.encode("utf-8")).decode("ascii")
        line = f"_pai_run {b64}\n".encode("ascii")
        _safe_write_log(sess, b"$ " + command.encode("utf-8", "replace") + b"\n")
        try:
            await _write_all_to_master(sess.master_fd, line)
        except OSError as e:
            return ShellResult(stdout="", stderr=f"write to bash failed: {e!r}", exit_code=-1)

        try:
            await asyncio.wait_for(sess.exit_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            try:
                os.write(sess.master_fd, b"\x03")  # ctrl-c via PTY foreground pgroup
            except Exception:
                pass
            await asyncio.sleep(0.1)
            stdout = bytes(sess.stdout_buf).decode("utf-8", "replace")
            stderr = bytes(sess.stderr_buf).decode("utf-8", "replace")
            if stderr:
                stderr += "\n"
            stderr += f"[pai] command timed out after {timeout}s, sent SIGINT"
            return ShellResult(stdout=stdout, stderr=stderr, exit_code=-1)

        await _drain_pipe_pending(sess)

        try:
            exit_code = int(bytes(sess.exit_buf).decode("ascii", "replace").strip().splitlines()[0])
        except Exception:
            exit_code = -1
        stdout = bytes(sess.stdout_buf).decode("utf-8", "replace")
        stderr = bytes(sess.stderr_buf).decode("utf-8", "replace")
        return ShellResult(stdout=stdout, stderr=stderr, exit_code=exit_code)


async def _spawn_session(slug: str, cwd: str, env: dict) -> _Session:
    master, slave = pty.openpty()
    try:
        _setup_slave_termios(slave)
        _set_winsize(master, _ROWS, _COLS)
        _set_nonblocking(master)
    except Exception:
        os.close(master); os.close(slave)
        raise

    exit_r, exit_w = os.pipe()
    out_r, out_w = os.pipe()
    err_r, err_w = os.pipe()
    for fd in (exit_r, out_r, err_r):
        _set_nonblocking(fd)

    proc_env = dict(env)
    proc_env["PAI_FD_EXIT"] = str(exit_w)
    proc_env["PAI_FD_OUT"] = str(out_w)
    proc_env["PAI_FD_ERR"] = str(err_w)
    proc_env["PS1"] = "$ "
    proc_env.setdefault("TERM", "xterm-256color")

    def _child_setup():
        # Make the PTY slave the controlling terminal of the new session.
        # start_new_session=True calls setsid(), which clears ctty; without
        # this, bash's `set -m` job control can't tcsetpgrp() and PTY-level
        # SIGINT (\x03) has no foreground pgroup to deliver to.
        try:
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)
        except Exception:
            pass

    proc = subprocess.Popen(
        ["bash", "--norc", "--noprofile"],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        cwd=cwd,
        env=proc_env,
        pass_fds=(exit_w, out_w, err_w),
        start_new_session=True,
        close_fds=True,
        preexec_fn=_child_setup,
    )
    # Parent doesn't need slave or write-ends.
    os.close(slave)
    os.close(exit_w); os.close(out_w); os.close(err_w)

    screen = pyte.Screen(_COLS, _ROWS)
    stream = pyte.ByteStream(screen)
    log_path = _live_log_path(slug)
    # Truncate live log per session boot.
    live_log = open(log_path, "wb", buffering=0)

    sess = _Session(
        slug=slug,
        proc=proc,
        master_fd=master,
        exit_r=exit_r,
        out_r=out_r,
        err_r=err_r,
        screen=screen,
        stream=stream,
        live_log=live_log,
    )

    loop = asyncio.get_running_loop()
    loop.add_reader(master, _on_master_readable, sess)
    loop.add_reader(out_r, _on_out_readable, sess)
    loop.add_reader(err_r, _on_err_readable, sess)
    loop.add_reader(exit_r, _on_exit_readable, sess)

    # Send init script (defines _pai_run). Bash will parse it before reading
    # the next line we send.
    try:
        os.write(master, _INIT_SCRIPT.encode("ascii"))
    except OSError as e:
        _force_teardown(sess)
        raise RuntimeError(f"bash init write failed for {slug}: {e!r}")

    _sessions[slug] = sess

    # Self-test: `_pai_run dHJ1ZQ==` (b64 of `true`) → fd 3 yields "0\n".
    try:
        result = await _exec_via_session(sess, "true", timeout=10)
    except Exception as e:
        _force_teardown(sess)
        raise RuntimeError(f"bash self-test crashed for {slug}: {e!r}")
    if result.exit_code != 0:
        _force_teardown(sess)
        raise RuntimeError(
            f"bash self-test failed for {slug}: rc={result.exit_code} "
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )

    _spawn_viewer_tmux(slug, log_path)
    return sess


def _spawn_viewer_tmux(slug: str, log_path: Path) -> None:
    """Spawn a detached tmux session whose only job is `tail -F <live_log>`,
    so the owner can attach with the same socket-based muscle memory and
    watch decoded commands + outputs in real time. Tmux is no longer in
    the kernel's parsing path; this is display-only.
    """
    sock = _socket_path(slug)
    target = f"pai-{slug}"
    try:
        subprocess.run(
            ["tmux", "-S", str(sock), "kill-session", "-t", target],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2,
        )
    except Exception:
        pass
    try:
        subprocess.run(
            [
                "tmux", "-S", str(sock), "new-session", "-d",
                "-s", target, "-x", "200", "-y", "50",
                "tail", "-F", str(log_path),
            ],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3,
        )
    except Exception:
        # Viewer is best-effort. Kernel keeps working without it.
        pass


def _force_teardown(sess: _Session) -> None:
    loop = None
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    for fd in (sess.master_fd, sess.exit_r, sess.out_r, sess.err_r):
        if loop is not None:
            try:
                loop.remove_reader(fd)
            except Exception:
                pass
        try:
            os.close(fd)
        except Exception:
            pass
    try:
        sess.proc.terminate()
    except Exception:
        pass
    try:
        sess.live_log.close()
    except Exception:
        pass
    _sessions.pop(sess.slug, None)


async def shutdown_all() -> None:
    """Tear down every live bash subprocess + viewer tmux. Called from
    the kernel's finally block on shutdown.
    """
    for sess in list(_sessions.values()):
        _force_teardown(sess)
    # The viewer tmux servers are killed by main.py's existing
    # tmux-*.sock sweep, so we don't duplicate that here.


def interrupt(slug: str) -> bool:
    """Send Ctrl-C to the foreground process group of the slug's PTY.

    Used by the kernel's nudge-cancel path to interrupt a long-running
    command without killing the persistent shell. The init script runs
    `set -m` so foreground commands sit in their own pgroup and bash
    itself doesn't share their SIGINT.
    """
    sess = _sessions.get(slug)
    if sess is None:
        return False
    try:
        os.write(sess.master_fd, b"\x03")
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Keys mode


_KEY_MAP: dict[str, bytes] = {
    "Enter": b"\r",
    "Tab": b"\t",
    "Escape": b"\x1b",
    "Space": b" ",
    "BSpace": b"\x7f",
    "Up": b"\x1b[A",
    "Down": b"\x1b[B",
    "Right": b"\x1b[C",
    "Left": b"\x1b[D",
    "PageUp": b"\x1b[5~",
    "PageDown": b"\x1b[6~",
    "Home": b"\x1b[H",
    "End": b"\x1b[F",
    "F1": b"\x1bOP", "F2": b"\x1bOQ", "F3": b"\x1bOR", "F4": b"\x1bOS",
    "F5": b"\x1b[15~", "F6": b"\x1b[17~", "F7": b"\x1b[18~",
    "F8": b"\x1b[19~", "F9": b"\x1b[20~", "F10": b"\x1b[21~",
    "F11": b"\x1b[23~", "F12": b"\x1b[24~",
}


def _parse_keys(keys: str) -> list[str]:
    """Whitespace-tokenize a keys string."""
    return keys.split()


def _token_to_bytes(token: str) -> bytes:
    if token in _KEY_MAP:
        return _KEY_MAP[token]
    if token.startswith("C-") and len(token) == 3:
        ch = token[2].lower()
        if "a" <= ch <= "z":
            return bytes([ord(ch) - ord("a") + 1])
        if ch == " ":
            return b"\x00"
    if token.startswith("M-") and len(token) >= 3:
        return b"\x1b" + token[2:].encode("utf-8")
    return token.encode("utf-8")


async def _send_keys_via_session(
    sess: _Session, keys: str, wait_ms: int
) -> ShellResult:
    async with sess.lock:
        tokens = _parse_keys(keys)
        if not tokens:
            return ShellResult(stdout="", stderr="empty keys", exit_code=-1)
        buf = b"".join(_token_to_bytes(t) for t in tokens)
        try:
            os.write(sess.master_fd, buf)
        except OSError as e:
            return ShellResult(stdout="", stderr=f"keys write failed: {e!r}", exit_code=-1)
        await asyncio.sleep(max(0, wait_ms) / 1000)

        # Snapshot pyte
        try:
            display_lines = list(sess.screen.display)
        except Exception:
            display_lines = []
        # Trim trailing blank lines for compactness
        while display_lines and not display_lines[-1].strip():
            display_lines.pop()
        body = "\n".join(line.rstrip() for line in display_lines)
        cursor = f"{sess.screen.cursor.x},{sess.screen.cursor.y}"
        if body:
            body = f"{body}\n[cursor {cursor}]"
        else:
            body = f"[cursor {cursor}]"
        return ShellResult(stdout=body, stderr="", exit_code=None)


# ---------------------------------------------------------------------------
# Public dispatcher


async def _get_or_spawn(slug: str, cwd: str, env: dict) -> _Session:
    sess = _sessions.get(slug)
    if sess is not None and sess.proc.poll() is None:
        return sess
    async with _spawn_lock:
        sess = _sessions.get(slug)
        if sess is not None and sess.proc.poll() is None:
            return sess
        if sess is not None:
            _force_teardown(sess)
        return await _spawn_session(slug, cwd, env)


async def run(
    tool_input: dict | str,
    env: Optional[dict] = None,
) -> ShellResult:
    if isinstance(tool_input, str):
        tool_input = {"command": tool_input}

    command = tool_input.get("command")
    keys = tool_input.get("keys")
    wait_ms_raw = tool_input.get("wait_ms")
    try:
        wait_ms = int(wait_ms_raw) if wait_ms_raw is not None else 300
    except (TypeError, ValueError):
        wait_ms = 300

    timeout_ms_raw = tool_input.get("timeout_ms")
    try:
        timeout_ms = int(timeout_ms_raw) if timeout_ms_raw is not None else _DEFAULT_TIMEOUT_MS
    except (TypeError, ValueError):
        timeout_ms = _DEFAULT_TIMEOUT_MS
    timeout_ms = max(1_000, min(_MAX_TIMEOUT_MS, timeout_ms))
    timeout_s = timeout_ms / 1000

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

    raw_slug = (env or {}).get("PAI_SLUG")
    slug = raw_slug or "pai"
    cwd = stitch.home_for(raw_slug) if raw_slug else HOME_DIR
    cwd.mkdir(parents=True, exist_ok=True)

    base_env = {**os.environ}
    base_env["PATH"] = build_pai_path(base_env.get("PATH", ""), host_first=True)
    base_env["TERM"] = "xterm-256color"
    proc_env = {**base_env, **env} if env else base_env

    try:
        sess = await _get_or_spawn(slug, str(cwd), proc_env)
    except Exception as e:
        return ShellResult(stdout="", stderr=f"bash spawn failed: {e!r}", exit_code=-1)

    if keys:
        return await _send_keys_via_session(sess, keys, wait_ms)
    hits = find_fhs_spellings(command, str(PAI_ROOT))
    if hits:
        log_fhs_reject(slug, hits)
        return ShellResult(
            stdout="",
            stderr="bash tool: " + fhs_reject_message(hits),
            exit_code=-1,
        )
    return await _exec_via_session(sess, command, timeout=timeout_s)
