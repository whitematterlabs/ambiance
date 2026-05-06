"""The single tool exposed to PAI: a bash shell rooted at home/.

Freesolo by design. cwd is home/; no path-escape filtering, no command
allowlist. The agent is trusted. If it runs `rm -rf` on its own world,
that's a PAI problem, not a harness problem.
"""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass
from typing import Optional

from . import stitch
from .paths import PAI_ROOT
from .processes import HOME_DIR

TOOL_NAME = "bash"
TOOL_DESCRIPTION = (
    "Run a bash command in PAI's world. PAI's filesystem is rooted at "
    "an FHS layout — `/etc/`, `/usr/`, `/var/`, `/proc/`, `/run/`, "
    "`/sys/`, `/boot/`, `/sbin/`, `/bin/`, `/opt/`, `/home/`, `/root/`, "
    "`/tmp/` all refer to PAI's world. Use absolute or relative paths "
    "freely; the harness rewrites FHS prefixes to PAI's root before "
    "exec. Output is captured stdout + stderr; exit code is reported."
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
    # Per-PAI cwd: each PAI's stitched home (root → /root/, others → /home/<slug>/).
    # Falls back to the global HOME_DIR if the caller didn't pass PAI_SLUG.
    slug = (env or {}).get("PAI_SLUG")
    cwd = stitch.home_for(slug) if slug else HOME_DIR
    cwd.mkdir(parents=True, exist_ok=True)
    command = rewrite_fhs_paths(command, str(PAI_ROOT))
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
    proc_env = {**base_env, **env} if env else base_env
    proc = await asyncio.create_subprocess_shell(
        command,
        cwd=str(cwd),
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
