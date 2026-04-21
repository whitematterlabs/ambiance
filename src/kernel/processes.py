"""Process primitives — spawn, resolve, read, log.

Every process is a directory in live/proc/{slug}/ containing spec.yaml,
status, and log.md. See src/KERNEL.md for the full spec.
"""

from datetime import datetime
from pathlib import Path

import yaml

LIVE_DIR = Path(__file__).resolve().parent.parent.parent / "live"
PROC_DIR = LIVE_DIR / "proc"
EVENTS_DIR = LIVE_DIR / "events"

VALID_STATUSES = {"spawned", "running", "completed", "expired", "cancelled"}


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


def emit_event(payload: dict) -> Path:
    """Write a YAML event file into live/events/. Consumed by the running kernel."""
    EVENTS_DIR.mkdir(parents=True, exist_ok=True)
    source = str(payload.get("source", "kernel"))
    # Microseconds + source keep filenames unique and debuggable.
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S%f")
    path = EVENTS_DIR / f"{stamp}-{source}.yaml"
    with path.open("w") as f:
        yaml.safe_dump(payload, f, sort_keys=False)
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

    emit_event({"source": "kernel", "kind": "process_spawned", "slug": slug})
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
    prev = (proc / "status").read_text().strip()
    (proc / "status").write_text(f"{new_status}\n")
    append_log(slug, f"kernel: resolved as {new_status}")

    if prev == "running" and new_status != "running":
        emit_event({"source": "kernel", "kind": "process_resolved", "slug": slug, "status": new_status})


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
