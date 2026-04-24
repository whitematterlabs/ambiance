"""The single tool exposed to PAI: a bash shell rooted at live/.

Freesolo by design. cwd is live/; no path-escape filtering, no command
allowlist. The agent is trusted. If it runs `rm -rf` on its own world,
that's a PAI problem, not a harness problem.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Optional

from .processes import LIVE_DIR

TOOL_NAME = "bash"
TOOL_DESCRIPTION = (
    "Run a bash command in PAI's world. The working directory IS the "
    "root of PAI's world — paths are relative to it; never prefix a "
    "directory name. Use this to read, search, and write files — cat, "
    "ls, rg, find, head, tail, echo >>, tee, mkdir, ln -s, etc. Output "
    "is captured stdout + stderr; exit code is reported separately."
)
TOOL_SCHEMA = {
    "name": TOOL_NAME,
    "description": TOOL_DESCRIPTION,
    "input_schema": {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The bash command to run.",
            },
        },
        "required": ["command"],
    },
}


@dataclass
class ShellResult:
    stdout: str
    stderr: str
    exit_code: int

    def render(self) -> str:
        out = self.stdout.rstrip("\n")
        err = self.stderr.rstrip("\n")
        parts = []
        if out:
            parts.append(out)
        if err:
            parts.append(f"[stderr]\n{err}")
        parts.append(f"[exit {self.exit_code}]")
        return "\n".join(parts)


async def run(
    command: str,
    timeout: float = 30.0,
    env: Optional[dict] = None,
) -> ShellResult:
    LIVE_DIR.mkdir(parents=True, exist_ok=True)
    proc_env = {**os.environ, **env} if env else None
    proc = await asyncio.create_subprocess_shell(
        command,
        cwd=str(LIVE_DIR),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=proc_env,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return ShellResult("", f"[timed out after {timeout}s]", 124)
    return ShellResult(
        stdout=stdout.decode("utf-8", errors="replace"),
        stderr=stderr.decode("utf-8", errors="replace"),
        exit_code=proc.returncode if proc.returncode is not None else -1,
    )
