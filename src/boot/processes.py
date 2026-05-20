"""Process primitives — spawn, resolve, read, log.

Every process is a directory in home/proc/{slug}/ containing spec.yaml,
status, and log.md. See src/usr/share/doc/KERNEL.md for the full spec.
"""

import os
import re
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml

from . import paths
from .paths import HOME_DIR, PAI_ROOT, PROC_DIR, EVENTS_DIR, ACKS_DIR

_METRICS_DIR = HOME_DIR / "sys" / "subagents"

# Pids reserved for kernel-seeded PAIs. Mirrors RESERVED_PIDS in config.py;
# duplicated here to avoid a circular import (config.py imports processes).
_RESERVED_PIDS = (1, 2)

VALID_STATUSES = {"spawned", "running", "completed", "expired", "cancelled", "failed", "stopped"}
TERMINAL_STATUSES = {"completed", "expired", "cancelled", "failed", "stopped"}

# Resolutions that wake PAI after the fact. "cancelled" is excluded because
# cancellation is typically initiated by PAI or the owner — the initiating
# turn is the right place to react, not a follow-up nudge.
NUDGE_ON_RESOLVE = {"completed", "expired", "failed"}


class ProcessExists(Exception):
    pass


class ProcessNotFound(Exception):
    pass


def _proc_dir(slug: str) -> Path:
    return PROC_DIR / slug


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _now_hm() -> str:
    return datetime.now().strftime("%H:%M")


def emit_event(payload: dict, target_pid: int | None = None) -> Path:
    """Write a YAML event file into $PAI_ROOT/run/pai/events/. Consumed by the running kernel.

    If `target_pid` is given, it is stamped onto the payload and the router
    delivers only to that pid, bypassing wake_on matching. Used by drivers
    that own per-PAI session state (e.g. ax) and need to address a specific
    PAI rather than fan out by event kind."""
    EVENTS_DIR.mkdir(parents=True, exist_ok=True)
    if target_pid is not None:
        payload = {**payload, "target_pid": int(target_pid)}
    source = str(payload.get("source", "kernel"))
    # Microseconds + source keep filenames unique and debuggable.
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S%f")
    path = EVENTS_DIR / f"{stamp}-{source}.yaml"
    # Atomic write: tmp + rename so watchdog sees a single CREATE event
    # instead of multiple deliveries across the open/write/close window.
    tmp = path.with_suffix(".yaml.tmp")
    with tmp.open("w") as f:
        yaml.safe_dump(payload, f, sort_keys=False)
    os.replace(tmp, path)
    return path


def emit_ack(msg_id: str, payload: dict) -> Path:
    """Write a per-msg delivery ack file under /run/pai/acks/<msg_id>.yaml.

    Senders (bin/send-message) poll this path with a short timeout.
    Lives outside EVENTS_DIR so the kernel watcher does not consume it."""
    ACKS_DIR.mkdir(parents=True, exist_ok=True)
    path = ACKS_DIR / f"{msg_id}.yaml"
    tmp = path.with_suffix(".yaml.tmp")
    with tmp.open("w") as f:
        yaml.safe_dump(payload, f, sort_keys=False)
    os.replace(tmp, path)
    return path


def spawn(slug: str, spec: dict) -> Path:
    """Create a new process directory with spec.yaml, status, log.md."""
    proc = _proc_dir(slug)
    if proc.exists():
        raise ProcessExists(f"process {slug!r} already exists at {proc}")

    spec = dict(spec)
    spec.setdefault("spawned", _now_iso())

    proc.mkdir(parents=True)
    with (proc / "spec.yaml").open("w") as f:
        yaml.safe_dump(spec, f, sort_keys=False)
    (proc / "status").write_text("running\n")
    (proc / "log.md").write_text(f"[{_now_hm()}] spawned\n")
    return proc


def resolve(slug: str, new_status: str) -> None:
    """Update a process's status and log the transition."""
    if new_status not in VALID_STATUSES:
        raise ValueError(
            f"invalid status {new_status!r}, expected one of {sorted(VALID_STATUSES)}"
        )
    proc = _proc_dir(slug)
    if not proc.exists():
        raise ProcessNotFound(slug)
    (proc / "status").write_text(f"{new_status}\n")
    append_log(slug, f"kernel: resolved as {new_status}")
    if new_status in NUDGE_ON_RESOLVE:
        payload = {
            "source": "kernel",
            "kind": "proc_resolved",
            "slug": slug,
            "status": new_status,
        }
        try:
            spec = read_spec(slug)
        except ProcessNotFound:
            spec = {}
        if "parent" in spec:
            payload["parent"] = spec["parent"]
        emit_event(payload)
    else:
        try:
            spec = read_spec(slug)
        except ProcessNotFound:
            spec = {}

    # Ephemeral subagents (kind: pai with a parent, not persub) are
    # self-contained — once resolved there's nothing to keep. Delete the
    # proc dir so they don't accumulate as zombies. Cron services spawned
    # with --parent must NOT match here: their proc dir has to survive
    # shutdown so rebuild_from_proc can re-arm the timer on next boot.
    if (
        spec.get("kind") == "pai"
        and "parent" in spec
        and not spec.get("persub")
        and new_status in TERMINAL_STATUSES
    ):
        try:
            _write_subagent_metrics(slug, spec, new_status)
        except Exception as e:
            print(f"[kernel] metrics: failed for {slug}: {e!r}", flush=True)
        # If this subagent owned a browse tab, mark the tab as orphan so a
        # future subagent can claim it. Tab stays open in Chrome.
        try:
            tab_file = PAI_ROOT / "sys" / "drivers" / "browse" / "tabs" / f"{slug}.yaml"
            if tab_file.exists():
                data = yaml.safe_load(tab_file.read_text()) or {}
                data["owner_status"] = "orphan"
                tab_file.write_text(yaml.safe_dump(data, sort_keys=False))
        except Exception as e:
            print(f"[kernel] browse-tab orphan mark failed for {slug}: {e!r}", flush=True)
        shutil.rmtree(proc, ignore_errors=True)


_SHELL_LINE_RE = re.compile(r"^\[pai:(?P<slug>[^\]]+)\] \$ (?P<cmd>.*)$")
_CLAUDE_P_RE = re.compile(r"\bclaude\s+-p\b")


def _write_subagent_metrics(slug: str, spec: dict, exit_status: str) -> None:
    """On subagent terminal exit, write /sys/subagents/<slug>/metrics.yaml.

    Pure telemetry — counts `claude -p` invocations in kernel.log lines
    tagged for this slug, plus duration and final status. No behavior
    change. The metric of interest is `claude_p_invocations`: a healthy
    coder run should be >= 1.
    """
    spawned = spec.get("spawned")
    duration_s: int | None = None
    if isinstance(spawned, str):
        try:
            t0 = datetime.fromisoformat(spawned)
            duration_s = int((datetime.now() - t0).total_seconds())
        except ValueError:
            pass

    invocations = 0
    log_path = PAI_ROOT / "var" / "log" / "kernel" / "kernel.log"
    if log_path.exists():
        prefix = f"[pai:{slug}] $ "
        try:
            with log_path.open("r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    if not line.startswith(prefix):
                        continue
                    if _CLAUDE_P_RE.search(line):
                        invocations += 1
        except Exception:
            pass

    metrics = {
        "slug": slug,
        "package": spec.get("package"),
        "duration_s": duration_s,
        "claude_p_invocations": invocations,
        "files_written": [],  # reserved; populate when we instrument writes
        "exit_status": exit_status,
    }

    target_dir = _METRICS_DIR / slug
    target_dir.mkdir(parents=True, exist_ok=True)
    with (target_dir / "metrics.yaml").open("w") as f:
        yaml.safe_dump(metrics, f, sort_keys=False)


def _iter_pai_specs():
    """Yield (slug, spec) for every kind:pai proc on disk."""
    if not PROC_DIR.exists():
        return
    for child in PROC_DIR.iterdir():
        if not child.is_dir() or child.name.startswith("."):
            continue
        spec_path = child / "spec.yaml"
        if not spec_path.exists():
            continue
        try:
            with spec_path.open() as f:
                spec = yaml.safe_load(f) or {}
        except Exception:
            continue
        if spec.get("kind") == "pai":
            yield child.name, spec


def _config_declared_pids() -> list[int]:
    """Pids declared in /etc/config.yaml. The reconcile may not have written
    /proc/<slug>/spec.yaml yet (first boot, or new entry not yet spawned),
    so config is the authoritative source for already-claimed pids."""
    cfg = paths.etc() / "config.yaml"
    try:
        with cfg.open() as f:
            data = yaml.safe_load(f) or {}
    except (FileNotFoundError, yaml.YAMLError):
        return []
    out: list[int] = []
    for entry in data.get("pais") or []:
        if not isinstance(entry, dict):
            continue
        pid = entry.get("pid")
        if isinstance(pid, int):
            out.append(pid)
    return out


def alloc_pai_pid() -> int:
    """Next free PID for a non-reserved kind:pai proc.

    Considers pids declared in /etc/config.yaml, pids recorded in
    /proc/*/spec.yaml, the reserved-pid set, and (for legacy specs lacking
    the field) the slug when it is all digits. Skips reserved pids — those
    belong to kernel-seeded PAIs (root=1, pai=2) and may not yet be on
    disk on first boot."""
    used: set[int] = set(_RESERVED_PIDS)
    for slug, spec in _iter_pai_specs():
        pid = spec.get("pid")
        if isinstance(pid, int):
            used.add(pid)
        elif slug.isdigit():
            used.add(int(slug))
    used.update(_config_declared_pids())
    candidate = 1
    while candidate in used:
        candidate += 1
    return candidate


def find_pai_slug(pid: int) -> str:
    """Return the proc-dir slug for the kind:pai proc with this PID.

    Matches `spec["pid"] == pid`, or the legacy slug==str(pid) shape for
    PAIs whose spec was written before the pid field existed."""
    for slug, spec in _iter_pai_specs():
        if spec.get("pid") == pid:
            return slug
        if "pid" not in spec and slug == str(pid):
            return slug
    raise ProcessNotFound(f"no kind:pai proc with pid={pid}")


def read_pai_pid(slug: str) -> int | None:
    """Return the PID recorded in this proc's spec, if any."""
    try:
        spec = read_spec(slug)
    except ProcessNotFound:
        return None
    pid = spec.get("pid")
    return pid if isinstance(pid, int) else None


def spawn_pai(
    pid: int = 1,
    slug: str | None = None,
    description: str = "Main PAI",
    *,
    prompt: str | None = None,
    prompt_dir: str | None = None,
    boilerplate: list[str] | None = None,
    provider: str | None = None,
    model: str | None = None,
    wake_on: list[str] | None = None,
    fallback: bool | None = None,
    parent: int | None = None,
    extra: dict | None = None,
) -> Path:
    """Spawn a `kind: pai` proc with an explicit PID. Slug defaults to
    str(pid) for the main PAI / back-compat; subagents pass a name.

    Optional fields are persisted into spec.yaml when provided.
    `prompt`/`wake_on` are honored by bootstrap.py and main.py;
    `provider`/`model` are read by nudge.py and routed to llm.run_turn."""
    if slug is None:
        slug = str(pid)
    spec: dict = {"kind": "pai", "pid": pid, "slug": slug, "description": description}
    if prompt is not None:
        spec["prompt"] = prompt
    if prompt_dir is not None:
        spec["prompt_dir"] = prompt_dir
    if boilerplate is not None:
        spec["boilerplate"] = list(boilerplate)
    if provider is not None:
        spec["provider"] = provider
    if model is not None:
        spec["model"] = model
    if wake_on is not None:
        spec["wake_on"] = list(wake_on)
    if fallback is not None:
        spec["fallback"] = bool(fallback)
    if parent is not None:
        spec["parent"] = parent
    if extra:
        for k, v in extra.items():
            spec.setdefault(k, v)
    return spawn(slug, spec)


def read_spec(slug: str) -> dict:
    proc = _proc_dir(slug)
    if not proc.exists():
        raise ProcessNotFound(slug)
    with (proc / "spec.yaml").open() as f:
        return yaml.safe_load(f) or {}


def read_status(slug: str) -> str:
    proc = _proc_dir(slug)
    if not proc.exists():
        raise ProcessNotFound(slug)
    return (proc / "status").read_text().strip()


def append_log(slug: str, message: str) -> None:
    proc = _proc_dir(slug)
    if not proc.exists():
        raise ProcessNotFound(slug)
    with (proc / "log.md").open("a") as f:
        f.write(f"[{_now_hm()}] {message}\n")


def mark_busy(slug: str, reason: str = "") -> None:
    """Flag a PAI as actively running a nudge. Presence-based: the file
    exists iff a nudge is in flight. Body is `reason\\n<unix_ts>` so the
    TUI can show what phase the nudge is in and how long it's been there.
    `_pai_locks` serializes nudges per PAI, so this is binary."""
    proc = _proc_dir(slug)
    if not proc.exists():
        raise ProcessNotFound(slug)
    (proc / "busy").write_text(f"{reason}\n{time.time()}\n")


def set_busy_reason(slug: str, reason: str) -> None:
    """Update the reason on an already-busy PAI without resetting the
    started_at timestamp. No-op if the PAI isn't currently busy."""
    proc = _proc_dir(slug)
    busy_file = proc / "busy"
    if not busy_file.exists():
        return
    started_at = ""
    try:
        existing = busy_file.read_text().splitlines()
        if len(existing) >= 2:
            started_at = existing[1].strip()
    except OSError:
        pass
    if not started_at:
        started_at = str(time.time())
    busy_file.write_text(f"{reason}\n{started_at}\n")


def clear_busy(slug: str) -> None:
    """Clear the busy flag. Idempotent — missing file is fine."""
    proc = _proc_dir(slug)
    (proc / "busy").unlink(missing_ok=True)


def is_busy(slug: str) -> bool:
    return (_proc_dir(slug) / "busy").exists()


def read_busy(slug: str) -> Optional[tuple[str, float]]:
    """Return (reason, started_at) for a busy PAI, or None if not busy.
    A malformed file (missing ts) returns (reason, 0.0)."""
    busy_file = _proc_dir(slug) / "busy"
    if not busy_file.exists():
        return None
    try:
        lines = busy_file.read_text().splitlines()
    except OSError:
        return None
    reason = lines[0].strip() if lines else ""
    started_at = 0.0
    if len(lines) >= 2:
        try:
            started_at = float(lines[1].strip())
        except ValueError:
            started_at = 0.0
    return reason, started_at


def list_procs(status_filter: str | None = None) -> list[str]:
    if not PROC_DIR.exists():
        return []
    slugs = []
    for child in sorted(PROC_DIR.iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        if status_filter is not None:
            status_file = child / "status"
            if not status_file.exists():
                continue
            if status_file.read_text().strip() != status_filter:
                continue
        slugs.append(child.name)
    return slugs


def show(slug: str) -> dict:
    """Return spec, status, and log contents for a process."""
    proc = _proc_dir(slug)
    if not proc.exists():
        raise ProcessNotFound(slug)
    return {
        "slug": slug,
        "spec": read_spec(slug),
        "status": read_status(slug),
        "log": (proc / "log.md").read_text(),
    }
