"""The web surface's writes — identical in shape to what the TUI writes.

Most writes mirror the TUI: append a line to a me-thread day-file and drop an
event file. This module also exposes explicit kernel lifecycle helpers for the
web header's start/stop control.
"""

from __future__ import annotations

import os
import fcntl
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import yaml

from . import voice
from bin import paiclone
from bin import paictl
from bin import paidel
from boot import config
from boot import paths
from boot.init import check_layout
from boot.paths import PAI_ROOT
from boot.nudge import apply_pending_history_action
from boot.processes import emit_event
from boot import stitch

from sbin.tui.state import HOME_DIR, today_file


PROVIDER_CONFIG_PATH = HOME_DIR / "memory" / "myself" / "provider.yaml"
PROVIDER_OPTIONS = [("Anthropic", "anthropic"), ("Deepseek", "deepseek"), ("OpenAI", "openai")]
_VALID_PROVIDERS = {k for _, k in PROVIDER_OPTIONS}
_KERNEL_LOCK_FILE = PAI_ROOT / "run" / "kernel.pid"


@dataclass(frozen=True, slots=True)
class SpeechAudio:
    data: bytes
    content_type: str


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
    env["PATH"] = paths.build_pai_path(env.get("PATH", ""), root=PAI_ROOT)

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


# --- text-to-speech / speech-to-text (voice) ---
#
# Engine-agnostic: these two functions keep their signatures (server.py calls
# them unchanged) but delegate to whatever voice provider package is installed
# and configured, resolved by `voice.resolve_provider`. The actual engines
# (ElevenLabs/OpenAI for cloud, whisper.cpp/`say` for local) live in those
# packages — `pai_web` no longer names an engine. The only residual engine here
# is the macOS-`say` last-resort below, so TTS never hard-fails when no voice
# package is installed.


def synthesize_speech(
    text: str,
    *,
    voice_id: str | None = None,
    speed: float | None = None,
) -> SpeechAudio:
    """Turn text into playable audio bytes via the resolved TTS provider.

    Dispatches to the installed/configured voice package (local `voice` →
    whisper/`say`, or `voice_cloud` → ElevenLabs). When no package provides TTS,
    falls back to the built-in macOS `say` last-resort so voice never hard-fails.

    Per-call ``voice_id`` / ``speed`` come from the browser; providers that
    ignore them (the local/`say` path) simply use the system voice.
    """
    provider = voice.resolve_provider("tts")
    if provider is not None:
        data, mime = provider.synthesize(text, voice_id=voice_id, speed=speed)
        return SpeechAudio(data, mime)
    return _synthesize_speech_macos_say(text)


def _synthesize_speech_macos_say(text: str) -> SpeechAudio:
    """Use macOS `say` with the user's default system voice."""
    say_path = shutil.which("say")
    if not say_path:
        raise RuntimeError("ELEVENLABS_API_KEY is not set and macOS 'say' is unavailable")
    afconvert_path = shutil.which("afconvert")
    if not afconvert_path:
        raise RuntimeError("ELEVENLABS_API_KEY is not set and macOS 'afconvert' is unavailable")

    with tempfile.TemporaryDirectory(prefix="pai-tts-") as tmp:
        aiff_output = Path(tmp) / "speech.aiff"
        m4a_output = Path(tmp) / "speech.m4a"
        _run_macos_audio_command(
            [say_path, "-o", str(aiff_output), "-f", "-"],
            input_text=text,
            label="macOS 'say'",
            timeout=60,
        )
        _run_macos_audio_command(
            [
                afconvert_path,
                "-f",
                "m4af",
                "-d",
                "aac",
                str(aiff_output),
                str(m4a_output),
            ],
            input_text=None,
            label="macOS 'afconvert'",
            timeout=30,
        )

        try:
            data = m4a_output.read_bytes()
        except FileNotFoundError as e:
            raise RuntimeError("macOS 'say' did not produce playable audio") from e

    if not data:
        raise RuntimeError("macOS 'say' produced empty playable audio")
    return SpeechAudio(data, "audio/mp4")


def _run_macos_audio_command(
    args: list[str],
    *,
    input_text: str | None,
    label: str,
    timeout: float,
) -> None:
    try:
        subprocess.run(
            args,
            input=input_text,
            text=True,
            capture_output=True,
            check=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"{label} timed out") from e
    except subprocess.CalledProcessError as e:
        detail = (e.stderr or e.stdout or "").strip()
        message = f"{label} failed"
        if detail:
            message += f": {detail}"
        raise RuntimeError(message) from e


def transcribe_speech(
    audio: bytes,
    *,
    filename: str,
    content_type: str,
    language: str | None = None,
    prompt: str | None = None,
) -> str:
    """Turn recorded browser audio into text via the resolved STT provider.

    Dispatches to the installed/configured voice package (local `voice` →
    whisper.cpp, or `voice_cloud` → OpenAI). Unlike TTS there is no built-in
    last-resort, so when no package provides STT this raises — the web surface
    surfaces it as "install a voice package".
    """
    provider = voice.resolve_provider("stt")
    if provider is None:
        raise RuntimeError(
            "no speech-to-text provider installed; run "
            "`paiman install voice` (local) or `paiman install voice_cloud`"
        )
    return provider.transcribe(
        audio,
        content_type=content_type,
        filename=filename,
        language=language,
        prompt=prompt,
    )


def _slug_for_pid(pid: int) -> str:
    from boot.processes import _iter_pai_specs

    for slug, spec in _iter_pai_specs():
        if spec.get("pid") == pid:
            return slug
    return str(pid)


def send_message(pid: int, text: str, *, overclock: bool = False) -> None:
    """Append `[HH:MM] me: text` to today's day-file, then wake the kernel."""
    path = today_file(pid)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = f"[{datetime.now().strftime('%H:%M')}] me: {text}\n"
    with path.open("a", encoding="utf-8") as f:
        f.write(line)
    payload = {
        "source": "web",
        "kind": "new_message",
        "thread": "me",
        "target_pid": pid,
        "text": text,
    }
    if overclock:
        payload["overclock"] = True
    emit_event(payload)


def interrupt(pid: int) -> None:
    emit_event({"source": "web", "kind": "interrupt", "pai": pid})


# Root is the privileged system PAI (reserved pid 1; see boot.config).
ROOT_PID = next((pid for pid, slug in config.RESERVED_PIDS.items() if slug == "root"), 1)

# The brief handed to root when the owner taps "Set up mobile access" in the
# header. Root is a capable agent — give it the objective + the facts that
# already hold + the one human-in-the-loop step (the ngrok authtoken), and let
# it work the rest out. Rendered as markdown in root's thread.
ROOT_REMOTE_SETUP_PROMPT = """\
The owner tapped **"Set up mobile access"** in the web console. Goal: make this \
PAI's web surface reachable from the owner's phone over the internet via an \
**ngrok** tunnel, so they can add it to their home screen as the PAI mobile app.

**What already exists — don't rebuild it:**
- The web surface can run as an authenticated remote TCP listener:
  `python -m usr.libexec.web.pai_web --port <PORT> --auth-token <TOKEN>`.
  With a token set, every `/api/*` route requires it (except `/api/health`).
- The frontend has a login gate: opening the tunnel URL with `?token=<TOKEN>`
  auto-authenticates (the QR path), or the owner types the token as an access code.
- It's a PWA — once open on the phone, "Add to Home Screen" installs it.

**What's missing — what you need to do:**
1. ngrok needs an account + authtoken (its API key). Check whether ngrok is
   installed (`which ngrok`); if not, install it (`brew install ngrok`). Check for
   an existing authtoken; if none is configured, ask the owner to create a free
   account at https://dashboard.ngrok.com and paste their authtoken, then run
   `ngrok config add-authtoken <token>`.
2. Pick a port and mint a strong random auth token. Start the authenticated
   remote web surface on that port, then `ngrok http <PORT>` to get a public
   https URL.
3. Build the mobile URL: `<public-url>?token=<TOKEN>`. Generate a QR code for it
   and show it to the owner (and print the URL + token in plain text as a
   fallback). Tell them to scan it on their phone and Add to Home Screen.
4. Make it durable: record how to relaunch the tunnel + surface and where the
   authtoken/token live, so next time is one step. Confirm with the owner once
   they're connected.

Work autonomously; only stop to ask the owner for the ngrok authtoken (step 1)
and to confirm they're connected (step 3).
"""


def setup_remote() -> dict:
    """Nudge root with the premade ngrok / mobile-access setup brief.

    Same two writes as any message (day-file line + new_message event), just
    addressed to root (pid 1) with a fixed prompt. The frontend focuses root's
    tab so the owner sees root's questions and the QR it produces.
    """
    send_message(ROOT_PID, ROOT_REMOTE_SETUP_PROMPT)
    return {"pid": ROOT_PID}


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


def delete_pai(name: str, *, stop_timeout: float = 10.0) -> dict:
    """Hard-purge a cloned fleet member: stop it, wait for it to drain, purge.

    A PAI is not an OS process — "running" is just `/proc/<name>/status`, and
    `paidel` refuses to delete a running PAI. Every PAI in the web UI is running,
    so delete is a *stop-then-purge* sequence: flip `active: false`, let the
    kernel resolve the proc, then tear it down (mirrors `paidel --purge`).

    Defense-in-depth: only clones are deletable. An entry without a `clone_of`
    marker is an original and is refused, matching the frontend which only shows
    the "−" button on clones.
    """
    name = name.strip()
    if not name:
        raise ValueError("missing PAI name")
    if config.clone_of(name) is None:
        raise ValueError(f"{name!r} is not a clone; refusing to delete")

    # Stop: flip active:false and ask the kernel to reconcile (resolves the
    # running proc). Same shape as `paictl stop`.
    try:
        paictl._set_active(name, False)
    except SystemExit as e:
        raise ValueError(str(e) or "failed to stop PAI") from e
    emit_event(
        {"kind": "kernel:reload_config", "source": "web", "action": "stop", "name": name}
    )

    # Wait for in-flight turns to drain — status flips off "running" once the
    # kernel resolves it. Idle PAIs flip sub-second; the timeout is a backstop.
    status_file = paths.proc(name) / "status"
    deadline = time.monotonic() + stop_timeout
    while time.monotonic() < deadline:
        try:
            running = status_file.read_text().strip().startswith("running")
        except FileNotFoundError:
            running = False
        if not running:
            break
        time.sleep(0.1)
    else:
        raise ValueError(f"{name!r} did not stop within {stop_timeout:.0f}s; try again")

    # Purge: drop the entry, rmtree home/proc/run + instance, final reload.
    try:
        result = paidel.delete(name, purge=True)
    except SystemExit as e:
        raise ValueError(str(e) or "paidel failed") from e
    return {
        "name": result.name,
        "home": str(result.home),
        "instance": str(result.instance),
        "purged": result.purged,
    }


def run_shell(pid: int, cmd: str) -> dict:
    """Run `cmd` with PAI's PATH/cwd/env. Returns {lines, rc, ctx_applied}.

    Output is transient (shown in the chat pane), never written to the thread —
    same as the TUI's `!cmd`.
    """
    slug = _slug_for_pid(pid)
    env = os.environ.copy()
    env["PATH"] = paths.build_pai_path(
        env.get("PATH", ""), root=PAI_ROOT, host_first=True
    )
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
