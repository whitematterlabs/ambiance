#!/usr/bin/env python
"""paiadd — instantiate a configured PAI from a bundle (useradd analogue).

Wizard. Prompts for the fields needed to build a fleet entry, then:
  1. creates the instance state dir at /var/lib/instances/<name>/
  2. stitches the home view at /home/<name>/ (or /root/ for pid 1)
  3. appends the entry to /etc/config.yaml
  4. emits a kernel:reload_config event

Usage:

    paiadd <bundle>          interactively configure a new instance
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import yaml

from boot import config as C
from boot import paths
from boot import processes as P
from boot import stitch


def _ask(prompt: str, default: str | None = None, *, required: bool = False) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        raw = input(f"{prompt}{suffix}: ").strip()
        if raw:
            return raw
        if default is not None:
            return default
        if not required:
            return ""
        print("  (required)")


def _ask_yn(prompt: str, default: bool = False) -> bool:
    suffix = "Y/n" if default else "y/N"
    raw = input(f"{prompt} [{suffix}]: ").strip().lower()
    if not raw:
        return default
    return raw in ("y", "yes")


def _known_kinds() -> dict[str, list[str]]:
    """Scan /usr/lib/drivers/*/events.yaml for emitted event kinds.

    Returns `{driver: [kind, ...]}`. The `kernel:*` namespace is
    implicit (emitted by the kernel itself, not by a driver) and is
    appended under the synthetic key 'kernel'."""
    out: dict[str, list[str]] = {}
    drivers_dir = paths.usr_lib_drivers()
    if drivers_dir.is_dir():
        for events_file in sorted(drivers_dir.glob("*/events.yaml")):
            with events_file.open() as f:
                data = yaml.safe_load(f) or {}
            kinds = [e["kind"] for e in data.get("events", []) if "kind" in e]
            if kinds:
                out[events_file.parent.name] = kinds
    out.setdefault("kernel", ["kernel:reload_config", "kernel:reload_failed", "kernel:proc_failed"])
    return out


def _print_kinds(kinds: dict[str, list[str]]) -> None:
    print("Available event kinds (use exact strings or fnmatch globs):")
    for driver, ks in kinds.items():
        print(f"  {driver}: {', '.join(ks)}")
    print("  e.g. 'gmail:*' matches every kind starting with 'gmail:'.")
    print("  Leave blank if this PAI should only be a `fallback` (catches unrouted events).")


def _existing_names(config_path: Path) -> set[str]:
    if not config_path.exists():
        return set()
    with config_path.open() as f:
        data = yaml.safe_load(f) or {}
    return {e["name"] for e in data.get("pais", []) if isinstance(e, dict) and "name" in e}


def _append_entry(config_path: Path, entry: dict) -> None:
    """Atomic append: read, mutate, .tmp + rename."""
    if config_path.exists():
        with config_path.open() as f:
            data = yaml.safe_load(f) or {}
    else:
        data = {}
    pais = data.setdefault("pais", [])
    pais.append(entry)
    tmp = config_path.with_suffix(config_path.suffix + ".tmp")
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with tmp.open("w") as f:
        yaml.safe_dump(data, f, sort_keys=False)
    tmp.rename(config_path)


def _build_entry(bundle: str, pkg: dict[str, Any]) -> dict[str, Any]:
    print(f"\nConfiguring instance from bundle {bundle!r}.")
    print(f"  package defaults: {dict(pkg)}\n")

    name = _ask("Instance name", default=bundle)
    if "/" in name or name.startswith(".") or not name:
        raise SystemExit(f"paiadd: invalid name {name!r}")

    existing = _existing_names(C.CONFIG_PATH)
    if name in existing:
        raise SystemExit(f"paiadd: {name!r} already in {C.CONFIG_PATH}")
    if paths.var_lib_instance(name).exists():
        raise SystemExit(
            f"paiadd: instance state already exists at {paths.var_lib_instance(name)} "
            "(use paidel --purge to remove first)"
        )

    description = _ask("Description", default=pkg.get("description") or None, required=True)
    provider = _ask("Provider", default=pkg.get("provider") or "anthropic")
    model = _ask("Model (blank = provider default)", default=pkg.get("model") or "")
    print()
    _print_kinds(_known_kinds())
    wake_on_raw = _ask(
        "Wake on (comma-separated)",
        default=",".join(pkg.get("wake_on") or []) or None,
    )
    wake_on = [g.strip() for g in wake_on_raw.split(",") if g.strip()]
    fallback = _ask_yn("Fallback (last-resort PAI)", default=False)

    entry: dict[str, Any] = {
        "name": name,
        "package": bundle,
        "description": description,
        "provider": provider,
    }
    if model:
        entry["model"] = model
    if wake_on:
        entry["wake_on"] = wake_on
    if fallback:
        entry["fallback"] = True
    return entry


def cmd_add(args: argparse.Namespace) -> int:
    bundle: str = args.bundle
    try:
        pkg = C.resolve_package(bundle)
    except C.ConfigError as e:
        raise SystemExit(f"paiadd: {e}")

    entry = _build_entry(bundle, pkg)

    print("\nFleet entry:")
    print(yaml.safe_dump([entry], sort_keys=False).rstrip())
    if not _ask_yn("\nProceed?", default=True):
        print("aborted.")
        return 1

    name = entry["name"]
    instance = paths.var_lib_instance(name)
    instance.mkdir(parents=True)
    home = stitch.stitch_home(name)
    _append_entry(C.CONFIG_PATH, entry)
    P.emit_event({"kind": "kernel:reload_config", "source": "paiadd", "added": name})

    print(f"\ninstance state: {instance}")
    print(f"home:           {home}")
    print(f"config:         {C.CONFIG_PATH} (entry appended)")
    print(f"\nNext: paictl start {name}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="paiadd", description=__doc__)
    ap.add_argument("bundle", help="bundle name under /usr/lib/pais/")
    ap.set_defaults(func=cmd_add)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
