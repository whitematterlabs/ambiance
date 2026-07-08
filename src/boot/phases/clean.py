"""Phase 2: clean — wipe ephemeral state from prior boots.

`tmp/` is system-wide ephemeral. `run/pai/events/` may hold stale event
files dropped by drivers between the kernel's last shutdown and this
boot. We do NOT wipe `proc/` here — process state is owned by the
proc-layer migration. Driver coroutines, however, cannot survive across
kernel boots, so a `kind: driver` proc left at `running` is stale until
the supervise loop starts it again.
"""
from __future__ import annotations

import os
import shutil
from datetime import datetime
from pathlib import Path

import yaml

# Import the module, not the name: PAI_ROOT is resolved at import time
# from os.environ. Tests reload boot.paths after monkeypatching PAI_ROOT;
# a `from ..paths import PAI_ROOT` would capture the pre-reload value.
from .. import paths


def _wipe_dir_contents(path: Path) -> None:
    if not path.is_dir():
        return
    for child in path.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()


def _wipe_busy_flags() -> None:
    """Drop any stale `busy` flags left by a prior crashed kernel. Each
    nudge writes /proc/<slug>/busy and clears it in a finally; if the
    kernel died mid-nudge, the flag is a phantom."""
    if not paths.PROC_DIR.is_dir():
        return
    for child in paths.PROC_DIR.iterdir():
        if not child.is_dir():
            continue
        (child / "busy").unlink(missing_ok=True)


def _append_proc_log(slug: str, line: str) -> None:
    proc = paths.PROC_DIR / slug
    try:
        hm = datetime.now().strftime("%H:%M")
        with (proc / "log.md").open("a") as f:
            f.write(f"[{hm}] {line}\n")
    except OSError:
        pass


def _emit_event(payload: dict) -> None:
    events = paths.EVENTS_DIR
    events.mkdir(parents=True, exist_ok=True)
    source = str(payload.get("source", "kernel"))
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S%f")
    path = events / f"{stamp}-{source}.yaml"
    tmp = path.with_suffix(".yaml.tmp")
    with tmp.open("w") as f:
        yaml.safe_dump(payload, f, sort_keys=False)
    os.replace(tmp, path)


def _find_pai_slug(pid: int) -> str | None:
    if not paths.PROC_DIR.is_dir():
        return None
    for child in paths.PROC_DIR.iterdir():
        if not child.is_dir():
            continue
        spec_path = child / "spec.yaml"
        if not spec_path.is_file():
            continue
        try:
            spec = yaml.safe_load(spec_path.read_text()) or {}
        except (OSError, yaml.YAMLError):
            continue
        if spec.get("kind") == "pai" and spec.get("pid") == pid:
            return child.name
    return None


def _int_or_none(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _is_stale_ad_hoc_subagent(proc: Path, spec: dict) -> bool:
    status_path = proc / "status"
    try:
        status = status_path.read_text().strip()
    except OSError:
        return False
    return (
        status == "running"
        and spec.get("kind") == "pai"
        and "parent" in spec
        and "run" not in spec
        and "schedule" not in spec
    )


def _mark_browse_tab_orphan(slug: str) -> None:
    tab_file = paths.PAI_ROOT / "sys" / "drivers" / "browse" / "tabs" / f"{slug}.yaml"
    if not tab_file.exists():
        return
    try:
        data = yaml.safe_load(tab_file.read_text()) or {}
        data["owner_status"] = "orphan"
        tab_file.write_text(yaml.safe_dump(data, sort_keys=False))
    except (OSError, yaml.YAMLError):
        pass


def _write_interruption_result(proc: Path, slug: str, parent_slug: str) -> str:
    dest_dir = paths.home_pai(parent_slug) / "workspace" / slug
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / "result.md"
    proc_result = proc / "result.md"
    if proc_result.is_file():
        shutil.copy2(proc_result, dest)
    else:
        dest.write_text(
            "# Subagent interrupted\n\n"
            f"`{slug}` was still running when the kernel booted, which means "
            "the prior kernel stopped or restarted before the subagent called "
            "`subagent done --result`.\n\n"
            "No final result was produced. Any Chrome tab owned by this "
            "subagent was left open and will be closed before the next "
            "browse subagent starts a fresh tab.\n"
        )
    return f"workspace/{slug}/result.md"


def _resolve_interrupted_subagents() -> None:
    """Turn stale running ad-hoc subagents into parent-visible failures.

    A graceful kernel restart cancels in-flight model turns after a bounded
    drain. Shutdown preserves ad-hoc subagent proc dirs so this boot phase,
    after wiping stale event files, can emit a fresh `subagent:response` that
    the new kernel will actually deliver.
    """
    if not paths.PROC_DIR.is_dir():
        return
    for proc in sorted(paths.PROC_DIR.iterdir()):
        if not proc.is_dir():
            continue
        spec_path = proc / "spec.yaml"
        if not spec_path.is_file():
            continue
        try:
            spec = yaml.safe_load(spec_path.read_text()) or {}
        except (OSError, yaml.YAMLError):
            continue
        if not _is_stale_ad_hoc_subagent(proc, spec):
            continue

        slug = proc.name
        parent_pid = _int_or_none(spec.get("parent"))
        child_pid = _int_or_none(spec.get("pid"))
        parent_slug = _find_pai_slug(parent_pid) if parent_pid is not None else None
        result_ref = None
        if parent_slug:
            result_ref = _write_interruption_result(proc, slug, parent_slug)

        _append_proc_log(slug, "boot: interrupted by prior kernel restart; notifying parent")
        _mark_browse_tab_orphan(slug)
        try:
            (proc / "status").write_text("failed\n")
        except OSError:
            pass

        if parent_pid is not None:
            text = "interrupted before completion"
            if result_ref:
                text = f"{text}: {result_ref}"
            payload = {
                "source": "kernel",
                "kind": "subagent:response",
                "target_pid": parent_pid,
                "text": text,
                "done": True,
            }
            if child_pid is not None:
                payload["sender_pid"] = child_pid
            if result_ref:
                payload["result"] = result_ref
            _emit_event(payload)

        shutil.rmtree(proc, ignore_errors=True)


def _reset_stale_driver_statuses() -> None:
    """Clear stale driver `running` statuses before boot hooks run.

    Drivers are in-kernel coroutines. At this point in boot none of them has
    been started yet, so `running` can only be leftover disk state from a
    prior unclean shutdown. Active drivers will be marked running again by
    `_reconcile_drivers()` after hooks and event-spool backfill complete.
    """
    if not paths.PROC_DIR.is_dir():
        return
    for child in paths.PROC_DIR.iterdir():
        if not child.is_dir():
            continue
        spec_path = child / "spec.yaml"
        status_path = child / "status"
        if not spec_path.is_file() or not status_path.is_file():
            continue
        try:
            spec = yaml.safe_load(spec_path.read_text()) or {}
        except (OSError, yaml.YAMLError):
            continue
        if spec.get("kind") != "driver":
            continue
        try:
            status = status_path.read_text().strip()
        except OSError:
            continue
        if status != "running":
            continue
        status_path.write_text("stopped\n")
        try:
            hm = datetime.now().strftime("%H:%M")
            with (child / "log.md").open("a") as f:
                f.write(f"[{hm}] boot: cleared stale running status\n")
        except OSError:
            pass


def run() -> None:
    _wipe_dir_contents(paths.PAI_ROOT / "tmp")
    _wipe_dir_contents(paths.EVENTS_DIR)
    _wipe_busy_flags()
    _resolve_interrupted_subagents()
    _reset_stale_driver_statuses()
    print(
        "[boot] clean: wiped tmp/, run/pai/events/, stale busy flags, "
        "and stale driver statuses",
        flush=True,
    )
