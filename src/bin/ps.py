#!/usr/bin/env python
"""ps — list /proc entries with parent/child tree.

Quick read-only view over the live process table. PAIs render as a tree
(subagents indent under their parent via spec[`parent`] → spec[`pid`]);
drivers render as a flat section below since they have no pid.

Usage:
    ps              tree view (default)
    ps -f           flat listing, one row per proc, sorted by pid
"""

from __future__ import annotations

import argparse
import sys

import yaml

from boot import processes as P
from boot.proctree import order_as_tree


def _scan() -> tuple[list[dict], list[dict]]:
    """Return (pais, drivers). Each entry is a dict with slug/pid/parent/status/description."""
    pais: list[dict] = []
    drivers: list[dict] = []
    if not P.PROC_DIR.exists():
        return pais, drivers
    for child in sorted(P.PROC_DIR.iterdir()):
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
        slug = child.name
        try:
            status = P.read_status(slug)
        except P.ProcessNotFound:
            status = "-"
        record = {
            "slug": slug,
            "kind": spec.get("kind", "?"),
            "pid": spec.get("pid"),
            "parent": spec.get("parent"),
            "active": spec.get("active", True),
            "status": status,
            "description": spec.get("description", ""),
        }
        if record["kind"] == "driver":
            drivers.append(record)
        else:
            pais.append(record)
    return pais, drivers


def _fmt_row(record: dict, prefix: str, by_pid: dict[int, dict]) -> str:
    pid = record.get("pid")
    pid_s = str(pid) if isinstance(pid, int) else "-"
    status = record.get("status", "-")
    slug = record["slug"]
    desc = record.get("description", "") or ""
    parent = record.get("parent")
    if isinstance(parent, int) and parent not in by_pid:
        desc = f"(orphan: parent {parent}) {desc}".rstrip()
    return f"{prefix}{pid_s:<5} {status:<10} {slug:<32} {desc}"


def cmd_tree(pais: list[dict], drivers: list[dict]) -> None:
    if not pais and not drivers:
        print("(no processes)")
        return

    if pais:
        by_pid = {r["pid"]: r for r in pais if isinstance(r.get("pid"), int)}
        print(f"{'PID':<5} {'STATUS':<10} {'SLUG':<32} DESCRIPTION")
        for record, prefix in order_as_tree(pais):
            print(_fmt_row(record, prefix, by_pid))

    if drivers:
        if pais:
            print()
        print("Drivers:")
        print(f"{'STATUS':<10} {'SLUG':<24} ACTIVE  DESCRIPTION")
        for d in sorted(drivers, key=lambda r: r["slug"]):
            active = "yes" if d.get("active", True) else "no"
            print(f"{d['status']:<10} {d['slug']:<24} {active:<6}  {d.get('description', '')}")


def cmd_flat(pais: list[dict], drivers: list[dict]) -> None:
    rows = pais + drivers
    if not rows:
        print("(no processes)")
        return
    print(f"{'PID':<5} {'KIND':<7} {'STATUS':<10} {'PARENT':<6} {'SLUG':<32} DESCRIPTION")
    rows.sort(key=lambda r: (r.get("pid") if isinstance(r.get("pid"), int) else 1 << 30, r["slug"]))
    for r in rows:
        pid = r.get("pid")
        pid_s = str(pid) if isinstance(pid, int) else "-"
        parent = r.get("parent")
        parent_s = str(parent) if isinstance(parent, int) else "-"
        print(f"{pid_s:<5} {r['kind']:<7} {r['status']:<10} {parent_s:<6} {r['slug']:<32} {r.get('description', '')}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ps", description="List /proc entries with parent/child tree.")
    ap.add_argument("-f", "--flat", action="store_true", help="flat listing, sorted by pid")
    args = ap.parse_args(argv)

    pais, drivers = _scan()
    if args.flat:
        cmd_flat(pais, drivers)
    else:
        cmd_tree(pais, drivers)
    return 0


if __name__ == "__main__":
    sys.exit(main())
