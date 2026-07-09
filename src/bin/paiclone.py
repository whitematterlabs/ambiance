#!/usr/bin/env python
"""paiclone — fork an existing fleet entry as a new instance.

Reads `/etc/config.yaml`, locates the entry by name, and appends a copy
under a fresh name. The clone shares prompt/bundle/model with the source
but starts with **no `wake_on` subscriptions** — a fresh clone is inert
until the owner gives it routing, so N identical catch-alls can't all
fire on every event (the pai-2/pai-3 load-amplification trap). Its
instance state (`/var/lib/instances/<new>/`) starts empty. The kernel
allocates a fresh pid on the next reconcile.

Usage:

    paiclone <source>                 auto-suffix (<source>-2, -3, …)
    paiclone <source> --name <new>    explicit new name
    paiclone <source> -y              skip confirm prompt

A clone of `root` is *not* a second pid-1 — it's a peer with root's
prompt and powers (no capability gates), stitched at `/home/<new>/`.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
import sys
from typing import Any

import yaml

from boot import config as C
from boot import paths
from bin import paiadd


@dataclass(frozen=True)
class ClonePlan:
    source: str
    name: str
    entry: dict[str, Any]


@dataclass(frozen=True)
class CloneResult(ClonePlan):
    instance: Path
    home: Path


def _load_entries() -> list[dict[str, Any]]:
    if not C.CONFIG_PATH.exists():
        raise SystemExit(f"paiclone: {C.CONFIG_PATH} not found")
    with C.CONFIG_PATH.open() as f:
        data = yaml.safe_load(f) or {}
    return [e for e in data.get("pais", []) if isinstance(e, dict)]


def _find_entry(entries: list[dict[str, Any]], name: str) -> dict[str, Any]:
    for e in entries:
        if e.get("name") == name:
            return e
    raise SystemExit(f"paiclone: no PAI named {name!r} in {C.CONFIG_PATH}")


def _next_free_name(base: str, taken: set[str]) -> str:
    # base-2, base-3, … — skip any already in config.
    i = 2
    while f"{base}-{i}" in taken:
        i += 1
    return f"{base}-{i}"


def plan_clone(source_name: str, new_name: str | None = None) -> ClonePlan:
    entries = _load_entries()
    taken = {e["name"] for e in entries if "name" in e}
    source = _find_entry(entries, source_name)

    new_name = new_name or _next_free_name(source_name, taken)
    if "/" in new_name or new_name.startswith(".") or not new_name:
        raise SystemExit(f"paiclone: invalid name {new_name!r}")
    if new_name in taken:
        raise SystemExit(f"paiclone: {new_name!r} already in {C.CONFIG_PATH}")
    if paths.var_lib_instance(new_name).exists():
        raise SystemExit(
            f"paiclone: instance state already exists at {paths.var_lib_instance(new_name)}"
        )

    # Shallow copy of source; rename and drop kernel-managed fields.
    entry: dict[str, Any] = dict(source)
    entry["name"] = new_name
    entry.pop("pid", None)  # let kernel allocate a fresh pid
    # Clones do NOT auto-inherit routing: they must not silently become a second
    # subscriber/catch-all. Both fields that route events are stripped —
    #   `wake_on`  — glob subscriptions (the B1 3×-amplification trap), and
    #   `fallback` — catch-all for unclaimed events; cloning the fallback PAI
    #                (e.g. `pai`) would otherwise fan every unclaimed event out
    #                to both the original and the clone.
    # The clone is inert until the owner assigns it routing explicitly.
    entry.pop("wake_on", None)
    entry.pop("fallback", None)
    # Behavior-free provenance marker: stamps this entry as a clone so surfaces
    # (the web "−" button, paidel guards) can tell clones from originals. It is
    # *not* the kernel's `parent` field — that flips a PAI into subagent identity.
    entry["clone_of"] = source_name

    return ClonePlan(source=source_name, name=new_name, entry=entry)


def materialize_clone(plan: ClonePlan) -> CloneResult:
    instance, home = paiadd.materialize(plan.entry)
    return CloneResult(
        source=plan.source,
        name=plan.name,
        entry=plan.entry,
        instance=instance,
        home=home,
    )


def clone(source_name: str, new_name: str | None = None) -> CloneResult:
    return materialize_clone(plan_clone(source_name, new_name))


def cmd_clone(args: argparse.Namespace) -> int:
    plan = plan_clone(args.source, args.name)

    print(f"Cloning {plan.source!r} → {plan.name!r}.")
    print("Fleet entry:")
    print(yaml.safe_dump([plan.entry], sort_keys=False).rstrip())

    if not args.yes:
        raw = input("\nProceed? [Y/n]: ").strip().lower()
        if raw and raw not in ("y", "yes"):
            print("aborted.")
            return 1

    result = materialize_clone(plan)
    print(f"\ninstance state: {result.instance}")
    print(f"home:           {result.home}")
    print(f"config:         {C.CONFIG_PATH} (entry appended)")
    print(f"\nNext: paictl start {result.name}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="paiclone", description=__doc__)
    ap.add_argument("source", help="name of existing PAI to clone")
    ap.add_argument("--name", help="new instance name (default: <source>-N)")
    ap.add_argument("-y", "--yes", action="store_true", help="skip confirm prompt")
    ap.set_defaults(func=cmd_clone)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
