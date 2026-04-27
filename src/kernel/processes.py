"""Process primitives — spawn, resolve, read, log.

Every process is a directory in home/proc/{slug}/ containing spec.yaml,
status, and log.md. See src/guides/KERNEL.md for the full spec.
"""

from datetime import datetime
from pathlib import Path

import yaml

HOME_DIR = Path(__file__).resolve().parent.parent.parent / "home"
PROC_DIR = HOME_DIR / "proc"
EVENTS_DIR = HOME_DIR / "events"

VALID_STATUSES = {"spawned", "running", "completed", "expired", "cancelled", "failed"}

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


def emit_event(payload: dict) -> Path:
    """Write a YAML event file into home/events/. Consumed by the running kernel."""
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


def alloc_pai_pid() -> int:
    """Next free PID across kind:pai procs. Reads `pid` from each spec; for
    legacy specs lacking the field, falls back to int(slug) when the slug is
    all digits. Default 1 if no PAIs exist."""
    pids: list[int] = []
    for slug, spec in _iter_pai_specs():
        pid = spec.get("pid")
        if isinstance(pid, int):
            pids.append(pid)
        elif slug.isdigit():
            pids.append(int(slug))
    return max(pids) + 1 if pids else 1


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
    model: str | None = None,
    wake_on: list[str] | None = None,
    fallback: bool | None = None,
    parent: int | None = None,
    extra: dict | None = None,
) -> Path:
    """Spawn a `kind: pai` proc with an explicit PID. Slug defaults to
    str(pid) for the main PAI / back-compat; subagents pass a name.

    Optional fields (prompt, model, wake_on) are persisted into spec.yaml
    when provided. `prompt` and `wake_on` are honored by bootstrap.py
    and main.py respectively; `model` is still inert (llm.py reads from
    provider.yaml)."""
    if slug is None:
        slug = str(pid)
    spec: dict = {"kind": "pai", "pid": pid, "slug": slug, "description": description}
    if prompt is not None:
        spec["prompt"] = prompt
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
