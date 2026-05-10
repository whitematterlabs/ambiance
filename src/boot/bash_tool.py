"""One-shot subprocess `bash` tool — the fast default.

A thin wrapper around `subprocess.run(["bash", "-c", command])` with no
PTY, no persistence, no tmux viewer, no side-channel pipes. State (cwd,
env) is *not* shared across calls — every invocation is a fresh child.

For persistent cwd/env, interactive TUIs, or sending raw keystrokes to a
foreground program, use the sibling `shell` tool instead.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Optional

from . import stitch
from ._shell_common import ShellResult, rewrite_fhs_paths
from .paths import PAI_ROOT
from .processes import HOME_DIR


TOOL_NAME = "bash"
TOOL_DESCRIPTION = (
    "Run a bash command in a fresh, isolated subprocess. No state is "
    "carried across calls — each invocation starts with a clean cwd "
    "and env. This is the default for one-shot work (`ls`, `git status`, "
    "build/test commands, scripts that finish on their own).\n\n"
    "If you need persistent cwd/env, an interactive TUI, or to send "
    "keystrokes to a foreground program, use the `shell` tool instead.\n\n"
    "PAI's filesystem is rooted at an FHS layout — `/etc/`, `/usr/`, "
    "`/var/`, `/proc/`, `/run/`, `/sys/`, `/boot/`, `/sbin/`, `/bin/`, "
    "`/opt/`, `/home/`, `/root/`, `/tmp/` all refer to PAI's world; "
    "FHS prefixes are rewritten to PAI's root before exec.\n\n"
    "Defaults: cwd is PAI's HOME; pass `cwd` to override (must exist). "
    "`timeout_ms` defaults to 120000 (2 min), max 600000 (10 min). On "
    "timeout the process is sent SIGTERM, then SIGKILL after a brief "
    "grace; partial stdout/stderr come back with exit_code -1.\n\n"
    "For long-running work (servers, watchers, slow batch jobs), use "
    "`shell` and background it with "
    "`nohup cmd > /tmp/<name>.log 2>&1 & echo $!` — `bash` cannot "
    "manage background PIDs across calls because it has no session."
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
                    "Bash command to run to completion. Runs in a fresh "
                    "subprocess; cwd/env do NOT persist across calls."
                ),
            },
            "cwd": {
                "type": "string",
                "description": (
                    "Optional working directory. Defaults to PAI's HOME. "
                    "Must exist; otherwise the call returns exit_code -1."
                ),
            },
            "timeout_ms": {
                "type": "integer",
                "description": (
                    "Wall-clock timeout in milliseconds. Default 120000 "
                    "(2 min). Maximum 600000 (10 min). On timeout the "
                    "process is terminated, partial stdout/stderr are "
                    "returned, and exit_code is -1."
                ),
            },
        },
        "required": ["command"],
    },
}


_DEFAULT_TIMEOUT_MS = 120_000
_MAX_TIMEOUT_MS = 600_000


def _build_env(extra: Optional[dict]) -> dict:
    """Build a clean env: PAI base process env + PATH prefix + TERM=dumb.

    Deliberately starts from `os.environ`, not from any state mutated by
    a persistent PTY session — the tool is fully isolated by design.
    """
    pai_path_prefix = os.pathsep.join([
        str(PAI_ROOT / "usr" / "lib" / "venv" / "bin"),
        str(PAI_ROOT / "usr" / "bin"),
        str(PAI_ROOT / "sbin"),
    ])
    base_env = {**os.environ}
    base_env["PATH"] = pai_path_prefix + os.pathsep + base_env.get("PATH", "")
    base_env["TERM"] = "dumb"
    base_env.pop("PS1", None)
    if extra:
        base_env.update(extra)
    return base_env


async def run(
    tool_input: dict | str,
    env: Optional[dict] = None,
) -> ShellResult:
    if isinstance(tool_input, str):
        tool_input = {"command": tool_input}

    command = tool_input.get("command")
    if not command or not isinstance(command, str):
        return ShellResult(
            stdout="", stderr="bash tool: `command` is required", exit_code=-1
        )

    timeout_ms_raw = tool_input.get("timeout_ms")
    try:
        timeout_ms = int(timeout_ms_raw) if timeout_ms_raw is not None else _DEFAULT_TIMEOUT_MS
    except (TypeError, ValueError):
        timeout_ms = _DEFAULT_TIMEOUT_MS
    timeout_ms = max(1_000, min(_MAX_TIMEOUT_MS, timeout_ms))
    timeout_s = timeout_ms / 1000

    raw_slug = (env or {}).get("PAI_SLUG")
    default_cwd = stitch.home_for(raw_slug) if raw_slug else HOME_DIR
    default_cwd.mkdir(parents=True, exist_ok=True)

    cwd_arg = tool_input.get("cwd")
    if cwd_arg:
        cwd = Path(cwd_arg)
        if not cwd.is_dir():
            return ShellResult(
                stdout="", stderr=f"bash tool: cwd does not exist: {cwd_arg}",
                exit_code=-1,
            )
    else:
        cwd = Path(default_cwd)

    proc_env = _build_env(env)

    rewritten = rewrite_fhs_paths(command, str(PAI_ROOT))

    try:
        proc = await asyncio.create_subprocess_exec(
            "bash", "-c", rewritten,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd),
            env=proc_env,
        )
    except Exception as e:
        return ShellResult(stdout="", stderr=f"bash spawn failed: {e!r}", exit_code=-1)

    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(), timeout=timeout_s
        )
    except asyncio.TimeoutError:
        try:
            proc.terminate()
        except ProcessLookupError:
            pass
        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=0.5)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                stdout_b, stderr_b = await proc.communicate()
            except Exception:
                stdout_b, stderr_b = b"", b""
        stdout = stdout_b.decode("utf-8", "replace") if stdout_b else ""
        stderr = stderr_b.decode("utf-8", "replace") if stderr_b else ""
        if stderr:
            stderr += "\n"
        stderr += f"[pai] command timed out after {timeout_s}s, terminated"
        return ShellResult(stdout=stdout, stderr=stderr, exit_code=-1)

    stdout = stdout_b.decode("utf-8", "replace") if stdout_b else ""
    stderr = stderr_b.decode("utf-8", "replace") if stderr_b else ""
    return ShellResult(stdout=stdout, stderr=stderr, exit_code=proc.returncode)
