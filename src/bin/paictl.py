#!/usr/bin/env python
"""paictl — control PAI instances and kernel-owned drivers.

Manages the runtime state of two things:
  - PAIs: declared in /etc/config.yaml `pais:`, source of truth on disk.
  - Drivers: declared by the kernel registry, runtime state in /proc/<slug>/.

The mechanism is one bit: an `active:` flag (default true) on the entry.
paictl flips it (in /etc/config.yaml for PAIs, in /proc/<slug>/spec.yaml
for drivers) and emits `kernel:reload_config`; the kernel's reconcile
takes care of the rest — spawning, stopping, status-healing.

For services (cron jobs, watchers, one-shot async work), see paicron.
For bundles, see paiman. For configuring/removing fleet members, see
paiadd / paidel.

Usage:

    paictl ls                  list fleet entries with active + runtime status
    paictl status NAME         show entry + /proc state
    paictl start NAME          set active: true, reload (spawns if needed)
    paictl stop NAME           set active: false, reload (resolves running proc)
    paictl logs NAME [-f]      print/tail /proc/<name>/log.md
    paictl reload              emit kernel:reload_config
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import yaml

from boot import config as C
from boot import paths
from boot import processes as P


def _load_raw(path: Path) -> dict:
    if not path.exists():
        raise SystemExit(f"paictl: {path} not found")
    with path.open() as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise SystemExit(f"paictl: {path} top level must be a mapping")
    return data


def _set_active(name: str, value: bool) -> bool:
    """Flip `active` on the named entry. Returns True if the file changed.

    Looks first in /etc/config.yaml `pais:` (PAIs are config-driven). If
    not found there, falls back to /proc/<name>/spec.yaml — that's where
    kernel-owned drivers live (they have no /etc/ entry; the kernel
    registry is their source of truth)."""
    data = _load_raw(C.CONFIG_PATH)
    pais = data.get("pais") or []
    if not isinstance(pais, list):
        raise SystemExit(f"paictl: {C.CONFIG_PATH}: `pais` must be a list")

    for entry in pais:
        if isinstance(entry, dict) and entry.get("name") == name:
            current = entry.get("active", True)
            if current == value:
                return False
            entry["active"] = value
            tmp = C.CONFIG_PATH.with_suffix(C.CONFIG_PATH.suffix + ".tmp")
            with tmp.open("w") as f:
                yaml.safe_dump(data, f, sort_keys=False)
            tmp.rename(C.CONFIG_PATH)
            return True

    spec_path = paths.proc(name) / "spec.yaml"
    if spec_path.exists():
        with spec_path.open() as f:
            spec = yaml.safe_load(f) or {}
        current = spec.get("active", True)
        if current == value:
            return False
        spec["active"] = value
        tmp = spec_path.with_suffix(spec_path.suffix + ".tmp")
        with tmp.open("w") as f:
            yaml.safe_dump(spec, f, sort_keys=False)
        tmp.rename(spec_path)
        return True

    raise SystemExit(f"paictl: {name!r} not found in pais or /proc/")


def _emit_reload(source: str, **extra: object) -> None:
    payload: dict[str, object] = {"kind": "kernel:reload_config", "source": source}
    payload.update(extra)
    P.emit_event(payload)


def _runtime_status(name: str) -> str:
    try:
        return P.read_status(name)
    except P.ProcessNotFound:
        return "-"


def cmd_start(args: argparse.Namespace) -> int:
    changed = _set_active(args.name, True)
    _emit_reload("paictl", action="start", name=args.name)
    print(f"{args.name}: active=true{' (no change)' if not changed else ''}, reload emitted")
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    changed = _set_active(args.name, False)
    _emit_reload("paictl", action="stop", name=args.name)
    print(f"{args.name}: active=false{' (no change)' if not changed else ''}, reload emitted")
    return 0


def cmd_reload(args: argparse.Namespace) -> int:
    _emit_reload("paictl")
    print("kernel:reload_config emitted")
    return 0


def _driver_rows() -> list[tuple[str, str, str, str]]:
    """Read /proc for kind:driver entries. Drivers have no /etc/ source;
    /proc is their source of truth."""
    rows: list[tuple[str, str, str, str]] = []
    proc_dir = P.PROC_DIR
    if not proc_dir.exists():
        return rows
    for child in sorted(proc_dir.iterdir()):
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
        if spec.get("kind") != "driver":
            continue
        name = child.name
        active = spec.get("active", True)
        rows.append((name, "yes" if active else "no", _runtime_status(name), ""))
    return rows


def cmd_ls(args: argparse.Namespace) -> int:
    data = _load_raw(C.CONFIG_PATH)
    pais = data.get("pais") or []
    pai_rows = []
    for entry in pais:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name", "?")
        active = entry.get("active", True)
        pai_rows.append((name, "yes" if active else "no", _runtime_status(name),
                         entry.get("description", "")))
    drv_rows = _driver_rows()

    if not pai_rows and not drv_rows:
        print("(no fleet entries)")
        return 0

    all_rows = pai_rows + drv_rows
    name_w = max(len(r[0]) for r in all_rows)
    status_w = max(len(r[2]) for r in all_rows)

    def _print_section(title: str, rows: list[tuple[str, str, str, str]]) -> None:
        if not rows:
            return
        print(f"\n{title}")
        print(f"{'NAME':<{name_w}}  ACTIVE  {'STATUS':<{status_w}}  DESCRIPTION")
        for name, active, status, desc in rows:
            print(f"{name:<{name_w}}  {active:<6}  {status:<{status_w}}  {desc}")

    _print_section("PAIs:", pai_rows)
    _print_section("Drivers:", drv_rows)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    data = _load_raw(C.CONFIG_PATH)
    pais = data.get("pais") or []
    entry = next((e for e in pais if isinstance(e, dict) and e.get("name") == args.name), None)
    if entry is None:
        # Fall back to /proc — drivers live there, not in /etc/config.yaml.
        spec_path = paths.proc(args.name) / "spec.yaml"
        if not spec_path.exists():
            raise SystemExit(f"paictl: {args.name!r} not found in pais or /proc/")
        with spec_path.open() as f:
            entry = yaml.safe_load(f) or {}

    print(f"name:    {args.name}")
    print(f"active:  {entry.get('active', True)}")
    print(f"status:  {_runtime_status(args.name)}")
    print("entry:")
    print(yaml.safe_dump(entry, sort_keys=False).rstrip())

    log_path = paths.proc(args.name) / "log.md"
    if log_path.exists():
        print("log (tail):")
        for line in log_path.read_text().splitlines()[-20:]:
            print(line)
    return 0


def cmd_logs(args: argparse.Namespace) -> int:
    log_path = paths.proc(args.name) / "log.md"
    if not log_path.exists():
        raise SystemExit(f"paictl: no log at {log_path}")
    print(log_path.read_text(), end="")
    if not args.follow:
        return 0
    pos = log_path.stat().st_size
    try:
        while True:
            time.sleep(0.5)
            cur = log_path.stat().st_size
            if cur > pos:
                with log_path.open() as f:
                    f.seek(pos)
                    sys.stdout.write(f.read())
                    sys.stdout.flush()
                pos = cur
    except KeyboardInterrupt:
        return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="paictl", description="Control PAI instances.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("ls", help="list fleet entries")
    p.set_defaults(func=cmd_ls)

    p = sub.add_parser("status", help="show entry + runtime status")
    p.add_argument("name")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("start", help="set active: true and reload")
    p.add_argument("name")
    p.set_defaults(func=cmd_start)

    p = sub.add_parser("stop", help="set active: false and reload")
    p.add_argument("name")
    p.set_defaults(func=cmd_stop)

    p = sub.add_parser("logs", help="print/tail /proc/<name>/log.md")
    p.add_argument("name")
    p.add_argument("-f", "--follow", action="store_true")
    p.set_defaults(func=cmd_logs)

    p = sub.add_parser("reload", help="emit kernel:reload_config")
    p.set_defaults(func=cmd_reload)

    args = ap.parse_args(argv)
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
