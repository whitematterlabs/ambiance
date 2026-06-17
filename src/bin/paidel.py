#!/usr/bin/env python
"""paidel — remove a configured PAI from the fleet (userdel analogue).

Default: removes the fleet entry and tears down the stitched home.
Instance state at /var/lib/instances/<name>/ is preserved (sacred —
re-adding restores memory/workspace/inbox).

`--purge` is the destructive variant: also wipes the instance state.

Usage:

    paidel <name>            remove fleet entry + home stitching
    paidel <name> --purge    also wipe /var/lib/instances/<name>/
"""

from __future__ import annotations

import argparse
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

from boot import config as C
from boot import paths
from boot import processes as P
from boot import stitch


def _is_running(name: str) -> bool:
    """Check /proc/<name>/status — the canonical running flag in this
    codebase (paictl writes it). Stale on uncleanly-killed kernels, but
    the user can `paictl stop` to flip it to 'stopped' and retry."""
    status_file = paths.proc(name) / "status"
    if not status_file.is_file():
        return False
    return status_file.read_text().strip().startswith("running")


def _drop_entry(config_path: Path, name: str) -> bool:
    """Atomic drop. Returns True if the entry was present."""
    if not config_path.exists():
        return False
    with config_path.open() as f:
        data = yaml.safe_load(f) or {}
    pais = data.get("pais") or []
    kept = [e for e in pais if not (isinstance(e, dict) and e.get("name") == name)]
    if len(kept) == len(pais):
        return False
    data["pais"] = kept
    tmp = config_path.with_suffix(config_path.suffix + ".tmp")
    with tmp.open("w") as f:
        yaml.safe_dump(data, f, sort_keys=False)
    tmp.rename(config_path)
    return True


@dataclass(frozen=True)
class DeleteResult:
    name: str
    home: Path
    proc_dir: Path
    instance: Path
    purged: bool


def delete(name: str, *, purge: bool = False) -> DeleteResult:
    """Tear down a fleet member: drop the config entry, rmtree home/proc/run,
    optionally purge instance state, then emit `kernel:reload_config`.

    Raises SystemExit (the CLI's error channel) if the PAI is still running or
    nothing matching `name` exists. Callers that want a softer error (the web
    surface) translate SystemExit into their own exception type."""
    home = stitch.home_for(name)
    instance = paths.var_lib_instance(name)
    proc_dir = paths.proc(name)
    run_dir = paths.run_pais(name)

    if _is_running(name):
        raise SystemExit(f"paidel: {name!r} is running; `paictl stop {name}` first")

    dropped = _drop_entry(C.CONFIG_PATH, name)
    targets = [home, proc_dir, run_dir]
    if purge:
        targets.append(instance)
    touched = dropped or any(t.exists() or t.is_symlink() for t in targets)
    if not touched:
        raise SystemExit(f"paidel: {name!r} not found in fleet")

    if home.exists() or home.is_symlink():
        shutil.rmtree(home)
    # /proc/<name>/ and /run/pais/<name>/ are declared-state mirrors that
    # reconcile writes into. Until a real PID-keyed proc layer lands, no
    # one else cleans these — paidel takes responsibility.
    if proc_dir.exists():
        shutil.rmtree(proc_dir)
    if run_dir.exists():
        shutil.rmtree(run_dir)

    purged = purge and instance.exists()
    if purged:
        shutil.rmtree(instance)

    P.emit_event({"kind": "kernel:reload_config", "source": "paidel", "removed": name})

    return DeleteResult(
        name=name, home=home, proc_dir=proc_dir, instance=instance, purged=purged
    )


def cmd_del(args: argparse.Namespace) -> int:
    result = delete(args.name, purge=args.purge)
    if result.purged:
        print(f"purged instance state at {result.instance}")
    print(f"removed fleet entry: {result.name}")
    print(f"removed home:        {result.home}")
    print(f"removed proc dir:    {result.proc_dir}")
    if not args.purge:
        print(f"instance preserved:  {result.instance}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="paidel", description=__doc__)
    ap.add_argument("name", help="instance name to remove")
    ap.add_argument(
        "--purge",
        action="store_true",
        help="also delete /var/lib/instances/<name>/ (destructive)",
    )
    ap.set_defaults(func=cmd_del)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
