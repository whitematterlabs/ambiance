#!/usr/bin/env python
"""paiadd — instantiate a configured PAI from a bundle (useradd analogue).

Wizard. Prompts for the fields needed to build a fleet entry, then:
  1. creates the instance state dir at /var/lib/instances/<name>/
  2. stitches the home view at /home/<name>/ (or /root/ for pid 1)
  3. appends the entry to /etc/config.yaml
  4. emits a kernel:reload_config event

Usage:

    paiadd <bundle>                              interactively configure
    paiadd <bundle> --yes [--name ...] [...]    non-interactive (for tool-use)

Non-interactive mode (`--yes`) skips all prompts. Any field not given as
a flag falls back to the bundle's package.yaml default. `--description`
is required (no sensible default). Use `--wake-on` repeatedly or comma-
separate; `--fallback` is a boolean flag.
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
    print("  e.g. 'email:*' matches every kind starting with 'email:'.")
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


def materialize(entry: dict[str, Any]) -> tuple[Path, Path]:
    """Realize a fleet entry on disk. Shared by paiadd and paiclone.

    Creates `/var/lib/instances/<name>/`, stitches `/home/<name>/` (or
    `/root/` for the reserved pid-1 name), appends the entry to
    `/etc/config.yaml`, and emits `kernel:reload_config`. The caller is
    responsible for having validated the entry first (unique name, no
    pre-existing instance dir).
    """
    name = entry["name"]
    instance = paths.var_lib_instance(name)
    instance.mkdir(parents=True)
    # Writable identity overlay: the librarian drops `*.md` here to evolve
    # (or override) this PAI's persona. Concatenated after the code-owned
    # base prompt by bootstrap; empty until something is written.
    (instance / "prompt").mkdir()
    home = stitch.stitch_home(name)
    _append_entry(C.CONFIG_PATH, entry)
    P.emit_event({"kind": "kernel:reload_config", "source": "paiadd", "added": name})
    return instance, home


def _build_entry_noninteractive(
    bundle: str, pkg: dict[str, Any], args: argparse.Namespace
) -> dict[str, Any]:
    name = args.name or bundle
    if "/" in name or name.startswith(".") or not name:
        raise SystemExit(f"paiadd: invalid name {name!r}")
    if name in _existing_names(C.CONFIG_PATH):
        raise SystemExit(f"paiadd: {name!r} already in {C.CONFIG_PATH}")
    if paths.var_lib_instance(name).exists():
        raise SystemExit(
            f"paiadd: instance state already exists at {paths.var_lib_instance(name)}"
        )

    description = args.description or pkg.get("description")
    if not description:
        raise SystemExit("paiadd: --description required (bundle has no default)")

    # --wake-on can repeat or be comma-separated; flatten both.
    wake_on: list[str] = []
    for raw in args.wake_on or []:
        wake_on.extend(g.strip() for g in raw.split(",") if g.strip())
    if not wake_on:
        wake_on = list(pkg.get("wake_on") or [])

    entry: dict[str, Any] = {
        "name": name,
        "package": bundle,
        "description": description,
        "provider": args.provider or pkg.get("provider") or "anthropic",
    }
    model = args.model if args.model is not None else pkg.get("model")
    if model:
        entry["model"] = model
    if wake_on:
        entry["wake_on"] = wake_on
    if args.fallback:
        entry["fallback"] = True
    return entry


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

    if args.yes:
        entry = _build_entry_noninteractive(bundle, pkg, args)
        print("Fleet entry:")
        print(yaml.safe_dump([entry], sort_keys=False).rstrip())
    else:
        entry = _build_entry(bundle, pkg)
        print("\nFleet entry:")
        print(yaml.safe_dump([entry], sort_keys=False).rstrip())
        if not _ask_yn("\nProceed?", default=True):
            print("aborted.")
            return 1

    instance, home = materialize(entry)

    print(f"\ninstance state: {instance}")
    print(f"home:           {home}")
    print(f"config:         {C.CONFIG_PATH} (entry appended)")
    print(f"\nNext: paictl start {entry['name']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="paiadd", description=__doc__)
    ap.add_argument("bundle", help="bundle name under /usr/lib/pais/")
    ap.add_argument("-y", "--yes", action="store_true", help="non-interactive; skip wizard and final confirm")
    ap.add_argument("--name", help="instance name (default: bundle name)")
    ap.add_argument("--description", help="instance description (default: bundle's)")
    ap.add_argument("--provider", help="LLM provider (default: bundle's)")
    ap.add_argument("--model", help="model id (default: bundle's, or provider default)")
    ap.add_argument("--wake-on", action="append", help="event-kind glob; repeat or comma-separate")
    ap.add_argument("--fallback", action="store_true", help="mark as last-resort PAI for unrouted events")
    ap.set_defaults(func=cmd_add)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
