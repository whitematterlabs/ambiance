"""The web surface's writes — identical in shape to what the TUI writes.

Two state-changing writes only: append a line to a me-thread day-file, and
drop an event file. Plus interrupt (an event) and provider selection (a config
file the kernel reads on the next turn). The shell runner mirrors the TUI's
`!cmd`. None of this owns or drives the kernel.
"""

from __future__ import annotations

import os
import subprocess
from datetime import datetime
from pathlib import Path

import yaml

from boot.paths import PAI_ROOT
from boot.nudge import apply_pending_history_action
from boot.processes import emit_event
from boot import stitch

from sbin.tui.state import HOME_DIR, today_file


PROVIDER_CONFIG_PATH = HOME_DIR / "memory" / "myself" / "provider.yaml"
PROVIDER_OPTIONS = [("Anthropic", "anthropic"), ("Deepseek", "deepseek")]
_VALID_PROVIDERS = {k for _, k in PROVIDER_OPTIONS}


def read_provider() -> str:
    try:
        data = yaml.safe_load(PROVIDER_CONFIG_PATH.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return "anthropic"
    key = data.get("provider") if isinstance(data, dict) else None
    return key if key in _VALID_PROVIDERS else "anthropic"


def write_provider(key: str) -> str:
    if key not in _VALID_PROVIDERS:
        raise ValueError(f"unknown provider: {key}")
    PROVIDER_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    PROVIDER_CONFIG_PATH.write_text(f"provider: {key}\n", encoding="utf-8")
    return key


def _slug_for_pid(pid: int) -> str:
    from boot.processes import _iter_pai_specs

    for slug, spec in _iter_pai_specs():
        if spec.get("pid") == pid:
            return slug
    return str(pid)


def send_message(pid: int, text: str) -> None:
    """Append `[HH:MM] me: text` to today's day-file, then wake the kernel."""
    path = today_file(pid)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = f"[{datetime.now().strftime('%H:%M')}] me: {text}\n"
    with path.open("a", encoding="utf-8") as f:
        f.write(line)
    emit_event(
        {
            "source": "web",
            "kind": "new_message",
            "thread": "me",
            "target_pid": pid,
            "text": text,
        }
    )


def interrupt(pid: int) -> None:
    emit_event({"source": "web", "kind": "interrupt", "pai": pid})


def run_shell(pid: int, cmd: str) -> dict:
    """Run `cmd` with PAI's PATH/cwd/env. Returns {lines, rc, ctx_applied}.

    Output is transient (shown in the chat pane), never written to the thread —
    same as the TUI's `!cmd`.
    """
    slug = _slug_for_pid(pid)
    env = os.environ.copy()
    pai_path = f"{PAI_ROOT / 'bin'}:{PAI_ROOT / 'usr' / 'bin'}"
    env["PATH"] = f"{pai_path}:{env.get('PATH', '')}"
    env["PAI_SLUG"] = slug
    env["PAI_ROOT"] = str(PAI_ROOT)

    try:
        proc = subprocess.run(
            cmd,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=str(stitch.home_for(slug)),
            env=env,
            timeout=120,
        )
        out = proc.stdout.decode(errors="replace").rstrip()
        rc = proc.returncode or 0
    except subprocess.TimeoutExpired:
        return {"lines": ["shell: timed out after 120s"], "rc": 124, "ctx_applied": False}
    except Exception as e:  # noqa: BLE001
        return {"lines": [f"shell: {e}"], "rc": 1, "ctx_applied": False}

    lines = out.splitlines() if out else []
    ctx_applied = False
    if rc == 0:
        ctx_applied = apply_pending_history_action(slug)
    return {"lines": lines, "rc": rc, "ctx_applied": ctx_applied}
