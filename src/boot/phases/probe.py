"""Phase 3: probe — driver health check.

For each driver shipped under /usr/lib/drivers/<name>/, confirm its
events.yaml is readable and the corresponding code module is
importable. Outputs one line per driver, never raises — a degraded
driver doesn't block boot, but it's logged loudly so kernelPAI can
self-heal once it's up.
"""
from __future__ import annotations

import importlib

import yaml

# Indirect import (see sanity.py for why).
from .. import paths


def _probe_one(driver_name: str) -> str:
    events_path = paths.usr_lib_drivers() / driver_name / "events.yaml"
    try:
        with events_path.open() as f:
            yaml.safe_load(f)
    except Exception as e:
        return f"ERR manifest unreadable ({e!r})"
    try:
        importlib.import_module(f"drivers.{driver_name}")
    except Exception as e:
        return f"ERR code not importable ({e!r})"
    return "ok"


def run() -> None:
    drivers_dir = paths.usr_lib_drivers()
    if not drivers_dir.is_dir():
        print("[boot] probe: no /usr/lib/drivers/ — skipping", flush=True)
        return
    for child in sorted(drivers_dir.iterdir()):
        if not child.is_dir():
            continue
        if not (child / "events.yaml").exists():
            continue
        name = child.name
        verdict = _probe_one(name)
        print(f"[boot] probe: {name} — {verdict}", flush=True)
