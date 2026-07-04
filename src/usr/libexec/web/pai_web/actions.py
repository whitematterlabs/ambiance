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
PROVIDER_OPTIONS = [("Anthropic", "anthropic"), ("Deepseek", "deepseek"), ("OpenAI", "openai"), ("GLM (z.ai)", "zai")]
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
**ngrok** tunnel, so they can add it to their home screen as the PAI mobile app. \
Two jobs:

**1. Get an ngrok API key (authtoken) yourself, using the owner's email account.**
Sign up for / log into ngrok (https://dashboard.ngrok.com) with the owner's
email and pull the authtoken from the dashboard — handle the email verification
through the owner's inbox yourself. Only stop to ask the owner if you hit
something you genuinely can't do (a password, 2FA, a captcha). Install ngrok
first if `which ngrok` is empty (`brew install ngrok`), then
`ngrok config add-authtoken <token>`.

**2. Set up a launch service so ngrok starts every time.**
Use `paicron` — our systemctl-shaped service manager — so the tunnel comes up on
every boot and restarts if it dies. Roughly:
`paicron start --slug ngrok --run 'ngrok http <PORT>' --restart always`,
and make it a boot hook (`paicron ensure`) so it survives reboots. Do the same
for the authenticated web surface so both come up together.

**What already exists — don't rebuild it:**
- The web surface runs as an authenticated remote TCP listener:
  `python -m usr.libexec.web.pai_web --port <PORT> --auth-token <TOKEN>`.
  With a token set, every `/api/*` route requires it (except `/api/health`).
- Opening the ngrok URL with `?token=<TOKEN>` auto-logs-in (the QR path), or the
  owner types the token as an access code. It's a PWA — "Add to Home Screen"
  installs it on the phone.

When both services are up, give the owner the mobile URL `<public-url>?token=<TOKEN>`
as a QR code (plus the URL + token in plain text as a fallback) and tell them to
scan it and Add to Home Screen. Work autonomously; only interrupt the owner for
the account step above and to confirm they're connected.
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


def kill_subagent(name: str) -> dict:
    """Abort a running subagent from the web UI (owner-initiated).

    A subagent is a `kind: pai` proc that carries a `parent`. Killing it must
    both stop its in-flight turn and mark it done: first `interrupt(pid)` to
    cancel the `_active_nudges` task actually running the subagent's work
    (same event the CLI's ESC-to-interrupt uses), then `processes.resolve(slug,
    "completed")` to flip its status, nudge the parent (proc_resolved), and let
    the kernel reap the ephemeral proc dir. Cancel-before-resolve so the task
    is stopped before it's marked done, not after. The fleet SSE then drops
    the tab once the proc disappears.

    Unlike the CLI path there is no parent-pid gate: the owner is allowed to
    abort any subagent. Persistent (`persub`) subagents are refused — they're
    declared in /etc/config.yaml and re-spawn, so killing them is meaningless.
    """
    from boot import processes as P

    name = name.strip()
    if not name:
        raise ValueError("missing subagent name")
    try:
        spec = P.read_spec(name)
    except P.ProcessNotFound:
        raise ValueError(f"no proc named {name!r}")
    if spec.get("kind") != "pai" or "parent" not in spec:
        raise ValueError(f"{name!r} is not a subagent")
    if spec.get("persub"):
        raise ValueError(f"{name!r} is a persistent subagent and cannot be killed")
    pid = spec.get("pid")
    if isinstance(pid, int):
        interrupt(pid)
    try:
        P.resolve(name, "completed")
    except P.ProcessNotFound:
        raise ValueError(f"{name!r} disappeared")
    return {"name": name}


# --- draft & approve: owner approval queue (web surface) -------------------
#
# A PAI under a send capability in `ask` mode sends normally; the outbound
# driver detects the gate and stages a `pending` record to
# var/spool/approvals/ instead of delivering. The web surface renders the
# queue and lets the owner approve or reject; the `approvals` driver watches
# the file and delivers anything the owner moves to `approved`. These helpers
# only flip status fields on the record — they never send, and (unlike the
# rest of this module) they never emit an event: the driver's own file
# watcher is the trigger. The secret grant token lives only in
# sys/drivers/approvals/ and never touches the queue record, so the review
# projection below leaks nothing.


def _approval_path(ident: str) -> Path:
    """Resolve `<ident>.yaml` in the approvals queue, rejecting any traversal.

    `ident` must be a bare stem — no separators, no `..` — so a crafted id
    can't escape the queue dir.
    """
    if not ident or ident != Path(ident).name or ".." in ident:
        raise ValueError(f"invalid approval id: {ident!r}")
    return paths.var_spool_approvals() / f"{ident}.yaml"


def _approval_dump(path: Path, data: dict) -> None:
    """Atomic rewrite (tmp + os.replace), same shape as the engine's dump."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True)
    os.replace(tmp, path)


def list_pending() -> list[dict]:
    """Review projection of every `pending` record, sorted by created_at.

    Exposes the actual attempted action (to/subject/body for email,
    thread/body for imessage) directly — there's no PAI-authored summary to
    fall back to, so the owner reads exactly what would go out.
    """
    out: list[dict] = []
    queue = paths.var_spool_approvals()
    if not queue.exists():
        return out
    for path in queue.glob("*.yaml"):
        try:
            rec = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            continue
        if not isinstance(rec, dict) or rec.get("status") != "pending":
            continue
        action = rec.get("action") or {}
        recipient = subject = body = ""
        if rec.get("channel") == "email":
            to = action.get("to") or []
            first = to[0] if isinstance(to, list) and to else ""
            recipient = first or action.get("in_reply_to") or ""
            subject = action.get("subject") or ""
            body = action.get("content") or ""
        elif rec.get("channel") == "imessage":
            recipient = action.get("thread") or ""
            body = action.get("text") or ""
        out.append(
            {
                "id": rec.get("id") or path.stem,
                "channel": rec.get("channel") or "",
                "created_by": rec.get("created_by") or "",
                "created_at": rec.get("created_at") or "",
                "recipient": recipient,
                "subject": subject,
                "body": body,
            }
        )
    out.sort(key=lambda r: r.get("created_at") or "")
    return out


def _decide(ident: str, status: str, *, error: str | None = None, body_override: str | None = None) -> dict:
    """Flip a still-`pending` record to a terminal owner decision.

    Terminal-guard: a record already decided (a double-click, or the driver
    having moved it on) is left untouched so we can't approve something twice.
    `body_override` merges an owner-edited body into the record's action
    before dispatch (`content` for email, `text` for imessage).
    """
    path = _approval_path(ident)
    try:
        rec = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"id": ident, "status": "missing", "error": "not found"}
    if not isinstance(rec, dict):
        return {"id": ident, "status": "missing", "error": "not found"}
    if rec.get("status") != "pending":
        return {"id": ident, "status": rec.get("status"), "error": "not pending"}
    if body_override is not None:
        action = rec.get("action") or {}
        key = "text" if rec.get("channel") == "imessage" else "content"
        action[key] = body_override
        rec["action"] = action
    rec["status"] = status
    rec["decided_at"] = datetime.now().isoformat(timespec="seconds")
    rec["decided_by"] = "owner"
    if status == "rejected":
        rec["error"] = error or None
    _approval_dump(path, rec)
    return {"id": ident, "status": status}


def approve_action(ident: str, body_override: str | None = None) -> dict:
    """Mark a pending record `approved`; the approvals driver delivers it."""
    return _decide(ident, "approved", body_override=body_override)


def reject_action(ident: str, reason: str = "") -> dict:
    """Mark a pending record `rejected`; nothing is sent."""
    return _decide(ident, "rejected", error=reason)


# --- send permissions: sidebar tri-state control ---------------------------
#
# The same `capabilities:` map the approval queue enforces, exposed as an
# owner-facing control. `list_send_capabilities` projects the current mode per
# *mounted* channel (a toggle for a channel no PAI can use would be a dead
# control); `set_send_mode` writes the choice back to config.yaml and asks the
# kernel to reload, which re-projects the driver freeze via
# `config.project_capabilities` — the exact mechanism a hand-edited config uses,
# so the sidebar and the file can never mean different things.

# Human labels for the send capabilities, keyed by their config flag.
SEND_CHANNEL_LABELS = {"email_send": "Email", "imessage_send": "iMessage"}


def _mounted_driver_union() -> set[str]:
    """Union of drivers mounted across the declared fleet.

    A send channel is only worth showing if some PAI can actually send on it.
    The owner-facing PAI is a fallback, so every *installed* driver lands here
    in practice — but computing the real union keeps the control honest on a
    fleet with no fallback."""
    try:
        slugs = list(config.load_config().keys())
    except Exception:  # noqa: BLE001 — a broken config shouldn't crash the surface
        return set()
    union: set[str] = set()
    for slug in slugs:
        try:
            union |= stitch.mounted_drivers_for(slug)
        except Exception:  # noqa: BLE001 — skip a PAI we can't resolve
            continue
    return union


def list_send_capabilities() -> list[dict]:
    """One row per mounted send channel: `{flag, channel, mode}`.

    Channels whose driver isn't mounted anywhere are omitted (no dead toggles).
    `mode` is the live config value (no/ask/yes), normalized on read."""
    mounted = _mounted_driver_union()
    modes = config.capability_modes()
    out: list[dict] = []
    for flag, spec in config.CAPABILITY_SPECS.items():
        if not (spec.get("mounts") or set()) & mounted:
            continue
        out.append(
            {
                "flag": flag,
                "channel": SEND_CHANNEL_LABELS.get(flag, flag),
                "mode": modes.get(flag, "no"),
            }
        )
    return out


def set_send_mode(flag: str, mode: str) -> dict:
    """Persist a send-mode choice and trigger a kernel reload.

    `config.set_capability_mode` is strict — it raises ValueError on an unknown
    flag or mode, which the server maps to a 400. On success the reload event
    makes `project_capabilities` re-write the driver freeze so the change takes
    effect without a restart (and, if the kernel is down, on its next boot)."""
    updated = config.set_capability_mode(flag, mode)
    emit_event(
        {
            "kind": "kernel:reload_config",
            "source": "web",
            "action": "send_mode",
            "flag": flag,
            "mode": updated,
        }
    )
    return {"flag": flag, "mode": updated}


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
