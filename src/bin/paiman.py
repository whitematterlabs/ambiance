#!/usr/bin/env python
"""paiman — PAI Package Manager.

Bundles are git-repo-shaped templates living at /usr/lib/pais/<name>/.
A bundle declares deps and ships a role prompt; instances are built from
bundles by `paiadd` (separate tool).

Usage:

    paiman init <name>     scaffold a new bundle template
    paiman install <url>   (deferred) clone a remote bundle
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from boot import paths


PACKAGE_YAML_TEMPLATE = """\
kind: pai
description: ""
prompt: usr/lib/pais/{name}/prompt.md
provider: anthropic
# model: claude-sonnet-4-6
#
# wake_on: list of fnmatch globs over event `kind:` strings. The kernel
# nudges this PAI when an event's kind matches any glob. Available kinds
# come from /usr/lib/drivers/<driver>/events.yaml plus the kernel:* namespace.
# Examples:
#   wake_on: ['gmail:*']            # every gmail driver event
#   wake_on: ['imessage:new']       # one specific kind
#   wake_on: ['gmail:*', 'cal:*']   # multiple globs
# Omit or leave empty if this PAI is only a `fallback` (catches unrouted).
# wake_on: []
#
# requires: deps that must exist in /usr/lib/drivers/ and /usr/lib/skills/
# before this bundle can be instantiated. paiman resolves these (deferred).
# requires:
#   drivers: []
#   skills: []
"""

PROMPT_MD_TEMPLATE = """\
# {name}

Role prompt for the {name} PAI.
"""


def _validate_name(name: str) -> None:
    # Mirrors src/boot/config.py:_validate_pai_entry name rules so a
    # scaffolded bundle is loadable by the config resolver.
    if not name:
        raise SystemExit("paiman: name must be non-empty")
    if "/" in name or name.startswith("."):
        raise SystemExit(f"paiman: invalid name {name!r}")


def cmd_init(args: argparse.Namespace) -> int:
    name: str = args.name
    _validate_name(name)
    bundle_dir: Path = paths.usr_lib_pais() / name
    if bundle_dir.exists():
        raise SystemExit(f"paiman: bundle already exists at {bundle_dir}")
    bundle_dir.mkdir(parents=True)
    (bundle_dir / "package.yaml").write_text(PACKAGE_YAML_TEMPLATE.format(name=name))
    (bundle_dir / "prompt.md").write_text(PROMPT_MD_TEMPLATE.format(name=name))
    print(f"scaffolded bundle at {bundle_dir}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="paiman", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="scaffold a new bundle template")
    p_init.add_argument("name", help="bundle name (e.g., email-pai)")
    p_init.set_defaults(func=cmd_init)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
