"""Driver health aggregation for the web console.

Pure read-side: folds together signals that are already on disk —

  - /usr/lib/drivers/<name>/**/events.yaml   what drivers exist, their process
                                             slugs, and optional `health:`
                                             staleness thresholds
  - /proc/<slug>/spec.yaml + status          kernel lifecycle (active flag,
                                             running/failed/cancelled)
  - /proc/<slug>/health.yaml                 supervision breadcrumbs written by
                                             boot.driver_health (starts, last
                                             start/exit, exit reason)
  - /sys/drivers/<name>/**                   newest file mtime = last-activity
                                             proxy ("is it actually ingesting")

and classifies each driver process into one of five states:

  ok       running with recent activity (green)
  stale    running, but no on-disk activity within its stale_after window (yellow)
  down     active but not running: crashed, failed to start, exited on its own,
           or never started (red)
  looping  respawning repeatedly — >= LOOP_THRESHOLD starts inside
           LOOP_WINDOW_S (red)
  off      deliberately disabled (active: false) — neutral, not an alarm

Staleness thresholds are per-driver-overridable in the shipped events.yaml:

    health:
      stale_after: 6h          # driver-wide
    processes:
      - slug: email-in
        health:
          stale_after: 2h      # per-process override

Ages are *computed at classification time from stored timestamps* — nothing
here ticks. The hub recomputes on FS events (plus its existing safety poll)
and change-gates the broadcast, so a staleness flip reaches the console
without any new transport.
"""

from __future__ import annotations

import os
import re
import time
from datetime import datetime
from pathlib import Path

import yaml

from boot import driver_health as breadcrumbs
from boot import paths
from boot import processes as P

# Default staleness window when a manifest doesn't declare one. A day of total
# silence from an ingest driver is worth a yellow dot; chattier expectations
# (imessage) or quieter ones (calendar) belong in the driver's events.yaml.
DEFAULT_STALE_AFTER_S = 24 * 3600

# Crash-loop detection: this many supervise starts inside the window is a loop.
LOOP_WINDOW_S = 30 * 60
LOOP_THRESHOLD = 3

# Bound the /sys/drivers/<name> mtime scan so a misbehaving driver that dumps
# thousands of files there can't wedge a recompute.
_MTIME_SCAN_MAX_FILES = 512

# Kernel-internal watcher tasks supervised like drivers but not shipped as
# /usr/lib/drivers bundles. They have no /sys state, so no staleness window —
# only down/looping matter for them.
_KERNEL_WATCHERS = ("proc-watcher", "doc-watcher")

_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*$", re.IGNORECASE)
_DURATION_UNITS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(val) -> int | None:
    """`90`, `"90s"`, `"15m"`, `"6h"`, `"3d"` → seconds. None/garbage → None."""
    if val is None:
        return None
    if isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        return int(val) if val > 0 else None
    m = _DURATION_RE.match(str(val))
    if not m:
        return None
    secs = float(m.group(1)) * _DURATION_UNITS[m.group(2).lower()]
    return int(secs) if secs > 0 else None


def _stale_after(manifest: dict, proc_entry: dict) -> int:
    """Per-process override beats driver-wide `health:` beats the default."""
    for scope in (proc_entry, manifest):
        health = scope.get("health")
        if isinstance(health, dict):
            secs = parse_duration(health.get("stale_after"))
            if secs is not None:
                return secs
    return DEFAULT_STALE_AFTER_S


def discover_processes() -> list[dict]:
    """Walk every events.yaml under /usr/lib/drivers (same walk the kernel's
    driver discovery does) and return one descriptor per process slug:
    {slug, driver, stale_after_s}. `driver` is the top-level bundle dir —
    that's the /sys/drivers/<name> key (sub-drivers like email/macmail share
    their parent's state dir)."""
    out: list[dict] = []
    drivers_dir = paths.usr_lib_drivers()
    if not drivers_dir.is_dir():
        return out
    for root, _dirs, files in os.walk(drivers_dir, followlinks=True):
        if "events.yaml" not in files:
            continue
        events_path = Path(root) / "events.yaml"
        try:
            with events_path.open() as f:
                manifest = yaml.safe_load(f) or {}
        except (OSError, yaml.YAMLError):
            continue
        if not isinstance(manifest, dict):
            continue
        rel = events_path.relative_to(drivers_dir)
        driver = rel.parts[0] if len(rel.parts) > 1 else str(manifest.get("driver") or "")
        for proc in manifest.get("processes") or []:
            if not isinstance(proc, dict) or not proc.get("slug"):
                continue
            out.append(
                {
                    "slug": str(proc["slug"]),
                    "driver": driver,
                    "stale_after_s": _stale_after(manifest, proc),
                }
            )
    out.sort(key=lambda d: (d["driver"], d["slug"]))
    return out


def newest_state_mtime(driver: str) -> float | None:
    """Newest file mtime under /sys/drivers/<driver>/ — the 'is it actually
    ingesting' proxy. None when the dir is missing or empty."""
    root = paths.sys_drivers(driver)
    newest: float | None = None
    seen = 0
    try:
        for dirpath, _dirs, files in os.walk(root):
            for name in files:
                seen += 1
                if seen > _MTIME_SCAN_MAX_FILES:
                    return newest
                try:
                    mt = os.stat(os.path.join(dirpath, name)).st_mtime
                except OSError:
                    continue
                if newest is None or mt > newest:
                    newest = mt
    except OSError:
        return newest
    return newest


def _iso_to_ts(iso) -> float | None:
    try:
        return datetime.fromisoformat(str(iso)).timestamp()
    except (ValueError, TypeError):
        return None


def classify(row: dict, now: float) -> tuple[str, str]:
    """(state, reason) for one assembled row. Pure — `now` is injected."""
    if not row["active"]:
        return "off", "disabled"
    status = row["status"]
    if status != "running":
        if not status:
            return "down", "no proc entry — never started"
        reason = row.get("last_exit_reason") or ""
        return "down", f"status {status}" + (f" — {reason}" if reason else "")
    # Status says running, but did the supervise task exit *after* its last
    # start? That's the silent-death tell (a clean `return` never resolves
    # /proc status). ISO timestamps compare lexicographically.
    last_start, last_exit = row.get("last_start"), row.get("last_exit")
    if last_start and last_exit and str(last_exit) >= str(last_start):
        outcome = row.get("last_exit_outcome") or "exited"
        reason = row.get("last_exit_reason") or ""
        return "down", f"task {outcome}" + (f" — {reason}" if reason else "")
    recent = [
        ts
        for ts in (_iso_to_ts(t) for t in row.get("recent_starts") or [])
        if ts is not None and now - ts <= LOOP_WINDOW_S
    ]
    if len(recent) >= LOOP_THRESHOLD:
        reason = row.get("last_exit_reason") or ""
        return "looping", f"{len(recent)} starts in {LOOP_WINDOW_S // 60}m" + (
            f" — {reason}" if reason else ""
        )
    stale_after = row.get("stale_after_s")
    activity = row.get("last_activity")
    if stale_after and activity is not None and now - activity > stale_after:
        since = datetime.fromtimestamp(activity).isoformat(timespec="seconds")
        return "stale", f"no activity since {since}"
    return "ok", ""


def _assemble(desc: dict, now: float, sys_mtimes: dict[str, float | None]) -> dict:
    slug, driver = desc["slug"], desc["driver"]
    try:
        spec = P.read_spec(slug)
    except Exception:
        spec = {}
    try:
        status = P.read_status(slug)
    except Exception:
        status = ""
    active_val = spec.get("active", True)
    active = bool(active_val) if isinstance(active_val, bool) else True
    health = breadcrumbs.read(slug)

    if driver not in sys_mtimes:
        sys_mtimes[driver] = newest_state_mtime(driver) if driver else None
    # Last activity: newest driver-state mtime, falling back to the task's own
    # last start so a just-(re)started driver isn't instantly "stale".
    candidates = [sys_mtimes[driver]] if driver else []
    candidates.append(_iso_to_ts(health.get("last_start")))
    known = [c for c in candidates if c is not None]
    last_activity = max(known) if known else None

    row = {
        "slug": slug,
        "driver": driver or slug,
        "active": active,
        "status": status,
        "starts": int(health.get("starts") or 0),
        "last_start": health.get("last_start"),
        "recent_starts": health.get("recent_starts") or [],
        "last_exit": health.get("last_exit"),
        "last_exit_outcome": health.get("last_exit_outcome"),
        "last_exit_reason": health.get("last_exit_reason") or "",
        "last_activity": last_activity,
        "stale_after_s": desc.get("stale_after_s"),
    }
    row["state"], row["state_reason"] = classify(row, now)
    # recent_starts fed classification; the console doesn't need the ring.
    del row["recent_starts"]
    return row


def read_rows(now: float | None = None) -> list[dict]:
    """One health row per driver process (manifest-declared slugs plus the
    kernel's own supervised watchers)."""
    now = time.time() if now is None else now
    sys_mtimes: dict[str, float | None] = {}
    descs = discover_processes()
    seen = {d["slug"] for d in descs}
    for slug in _KERNEL_WATCHERS:
        if slug not in seen:
            descs.append({"slug": slug, "driver": "", "stale_after_s": None})
    return [_assemble(d, now, sys_mtimes) for d in descs]
