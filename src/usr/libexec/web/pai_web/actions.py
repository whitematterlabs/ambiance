"""The web surface's writes — identical in shape to what the TUI writes.

Most writes mirror the TUI: append a line to a me-thread day-file and drop an
event file. This module also exposes explicit kernel lifecycle helpers for the
web header's start/stop control.
"""

from __future__ import annotations

import os
import fcntl
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
import yaml

from bin import paiclone
from boot.init import check_layout
from boot.paths import PAI_ROOT
from boot.nudge import apply_pending_history_action
from boot.processes import emit_event
from boot import stitch

from sbin.tui.state import HOME_DIR, today_file


PROVIDER_CONFIG_PATH = HOME_DIR / "memory" / "myself" / "provider.yaml"
PROVIDER_OPTIONS = [("Anthropic", "anthropic"), ("Deepseek", "deepseek")]
_VALID_PROVIDERS = {k for _, k in PROVIDER_OPTIONS}
_KERNEL_LOCK_FILE = PAI_ROOT / "run" / "kernel.pid"


def _kernel_python() -> str:
    """Return the interpreter that should boot the kernel.

    The web surface may run from a repo/dev venv, but the kernel needs the FHS
    runtime where `/usr/lib` drivers and `/usr/src` are importable.
    """
    fhs_python = PAI_ROOT / "usr" / "lib" / "venv" / "bin" / "python"
    return str(fhs_python if fhs_python.exists() else Path(sys.executable))


def _kernel_env() -> dict[str, str]:
    env = os.environ.copy()
    env["PAI_ROOT"] = str(PAI_ROOT)

    prefix = os.pathsep.join(
        [
            str(PAI_ROOT / "usr" / "lib" / "venv" / "bin"),
            str(PAI_ROOT / "usr" / "bin"),
            str(PAI_ROOT / "sbin"),
        ]
    )
    current_path = env.get("PATH", "")
    if current_path != prefix and not current_path.startswith(prefix + os.pathsep):
        env["PATH"] = prefix + (os.pathsep + current_path if current_path else "")

    python_roots = [str(PAI_ROOT / "usr" / "lib"), str(PAI_ROOT / "usr" / "src")]
    current_pythonpath = env.get("PYTHONPATH")
    if current_pythonpath:
        python_roots.append(current_pythonpath)
    env["PYTHONPATH"] = os.pathsep.join(python_roots)
    return env


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


def kernel_status() -> dict:
    """Return whether the kernel lock is currently held."""
    if not _KERNEL_LOCK_FILE.exists():
        return {"running": False, "pid": None}
    try:
        fd = os.open(_KERNEL_LOCK_FILE, os.O_RDWR)
    except OSError:
        return {"running": False, "pid": None}
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            try:
                pid = os.read(fd, 64).decode().strip() or None
            except OSError:
                pid = None
            return {"running": True, "pid": pid}
        fcntl.flock(fd, fcntl.LOCK_UN)
        return {"running": False, "pid": None}
    finally:
        os.close(fd)


def _wait_for_kernel(running: bool, timeout: float = 4.0) -> dict:
    deadline = time.monotonic() + timeout
    status = kernel_status()
    while status["running"] is not running and time.monotonic() < deadline:
        time.sleep(0.1)
        status = kernel_status()
    return status


def start_kernel() -> dict:
    """Start the kernel in the background if it is not already running."""
    status = kernel_status()
    if status["running"]:
        return status
    missing = check_layout(PAI_ROOT)
    if missing:
        raise RuntimeError(
            f"PAI_ROOT={PAI_ROOT} missing required dirs: {', '.join(missing)}; "
            "run `paifs-init` to lay out the skeleton"
        )

    log_path = PAI_ROOT / "var" / "log" / "kernel" / "kernel.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_fp = log_path.open("a", buffering=1, encoding="utf-8")
    subprocess.Popen(
        [_kernel_python(), "-u", "-m", "boot.entry"],
        start_new_session=True,
        stdout=log_fp,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        env=_kernel_env(),
    )
    status = _wait_for_kernel(True)
    if not status["running"]:
        raise RuntimeError(f"kernel did not start within 4s; see {log_path}")
    return status


def stop_kernel() -> dict:
    """Ask the kernel to shut down, escalating only if it stays locked."""
    status = kernel_status()
    if not status["running"]:
        return status
    pid_raw = status.get("pid")
    try:
        pid = int(pid_raw)
    except (TypeError, ValueError):
        raise RuntimeError("kernel is running but pid is unknown")

    try:
        pgid = os.getpgid(pid)
    except ProcessLookupError:
        return _wait_for_kernel(False)

    try:
        if pgid == pid:
            os.killpg(pgid, signal.SIGTERM)
        else:
            os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return _wait_for_kernel(False)

    status = _wait_for_kernel(False, timeout=6.0)
    if status["running"]:
        if pgid == pid:
            os.killpg(pgid, signal.SIGKILL)
        else:
            os.kill(pid, signal.SIGKILL)
        status = _wait_for_kernel(False, timeout=2.0)
    if status["running"]:
        raise RuntimeError(f"kernel did not stop (pid={status.get('pid') or pid})")
    return status


# --- text-to-speech (voice mode) ---
#
# The single server-side swap point for voice mode. Replacing ElevenLabs with
# another engine (an audio LLM, the system voice) means editing only this
# function — no ElevenLabs detail leaks into server.py. The API key stays here
# and never reaches the browser; the frontend POSTs text to /api/tts.

_ELEVENLABS_TTS_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
_DEFAULT_VOICE_ID = "21m00Tcm4TlvDq8ikWAM"  # Rachel
_DEFAULT_MODEL_ID = "eleven_turbo_v2_5"  # low-latency
_OPENAI_TRANSCRIPTIONS_URL = "https://api.openai.com/v1/audio/transcriptions"
_DEFAULT_TRANSCRIBE_MODEL = "gpt-4o-mini-transcribe"


def _reload_dotenv() -> None:
    """Re-read .env.local / .env from $PAI_ROOT and the repo, same order as
    boot/__init__.py. Lets the user drop a fresh key into the file mid-session
    and have voice mode work on the next request without restarting the server.
    override=False matches boot's behavior so a shell-exported value still wins.
    """
    from dotenv import load_dotenv

    pai_root = Path(os.environ.get("PAI_ROOT", str(Path.home() / ".pai")))
    code_root = Path(__file__).resolve().parents[5]
    for base in (pai_root, code_root):
        load_dotenv(base / ".env.local")
        load_dotenv(base / ".env")


def synthesize_speech(text: str) -> bytes:
    """Swap point: turn text into mp3 bytes. v1 = ElevenLabs.

    Reads ELEVENLABS_API_KEY (required), ELEVENLABS_VOICE_ID and
    ELEVENLABS_MODEL_ID (optional) from the environment (loaded from
    ~/.pai/.env.local by boot/__init__.py). Raises RuntimeError if the key is
    missing so the route can return 400.
    """
    api_key = os.environ.get("ELEVENLABS_API_KEY")
    if not api_key:
        _reload_dotenv()
        api_key = os.environ.get("ELEVENLABS_API_KEY")
    if not api_key:
        raise RuntimeError("ELEVENLABS_API_KEY is not set")
    voice_id = os.environ.get("ELEVENLABS_VOICE_ID") or _DEFAULT_VOICE_ID
    model_id = os.environ.get("ELEVENLABS_MODEL_ID") or _DEFAULT_MODEL_ID

    resp = requests.post(
        _ELEVENLABS_TTS_URL.format(voice_id=voice_id),
        headers={"xi-api-key": api_key, "accept": "audio/mpeg"},
        params={"output_format": "mp3_44100_128"},
        json={"text": text, "model_id": model_id},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.content


def transcribe_speech(
    audio: bytes,
    *,
    filename: str,
    content_type: str,
    language: str | None = None,
    prompt: str | None = None,
) -> str:
    """Swap point: turn recorded browser audio into text. v1 = OpenAI STT."""
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set")
    if not audio:
        raise ValueError("empty audio")

    model = os.environ.get("OPENAI_TRANSCRIBE_MODEL") or _DEFAULT_TRANSCRIBE_MODEL
    data = {"model": model, "response_format": "json"}
    configured_language = language or os.environ.get("OPENAI_TRANSCRIBE_LANGUAGE")
    configured_prompt = prompt or os.environ.get("OPENAI_TRANSCRIBE_PROMPT")
    if configured_language:
        data["language"] = configured_language
    if configured_prompt:
        data["prompt"] = configured_prompt

    resp = requests.post(
        _OPENAI_TRANSCRIPTIONS_URL,
        headers={"Authorization": f"Bearer {api_key}"},
        data=data,
        files={"file": (filename, audio, content_type)},
        timeout=60,
    )
    resp.raise_for_status()
    try:
        payload = resp.json()
    except ValueError as e:
        raise RuntimeError("transcription response was not JSON") from e
    text = payload.get("text")
    if not isinstance(text, str):
        raise RuntimeError("transcription response missing text")
    return text.strip()


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


def clone_pai(source: str) -> dict:
    """Clone a fleet member through the same implementation as PAI.app/CLI."""
    source = source.strip()
    if not source:
        raise ValueError("missing source PAI")
    try:
        result = paiclone.clone(source)
    except SystemExit as e:
        msg = str(e) or "paiclone failed"
        raise ValueError(msg) from e
    return {
        "source": result.source,
        "name": result.name,
        "instance": str(result.instance),
        "home": str(result.home),
    }


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
