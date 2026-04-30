#!/usr/bin/env python
"""paiman — PAI Package Manager.

Bundles are git-repo-shaped templates. Two types today:

    pai       — fleet-member bundles at /usr/lib/pais/<name>/
    subagent  — persub specialist bundles at /usr/lib/subagents/<name>/

A bundle declares deps and ships a role prompt; pai instances are built
from pai bundles by `paiadd`. Subagent bundles are referenced from a
parent's `dependencies:` entry via `package: <name>`.

Usage:

    paiman init <name> [--type pai|subagent]   scaffold a new bundle
    paiman list                                 list installed bundles
    paiman show <name>                          print resolved package.yaml
    paiman install <url>                        (deferred) clone a remote bundle
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

from boot import paths


PAI_PACKAGE_YAML_TEMPLATE = """\
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

SUBAGENT_PACKAGE_YAML_TEMPLATE = """\
kind: subagent
description: ""
prompt: usr/lib/subagents/{name}/prompt.md
provider: anthropic
# model: claude-sonnet-4-6
#
# Subagent bundles are referenced from a parent's dependencies: entry
# via `package: {name}`. They have no wake_on/fallback — the parent
# addresses them directly via bin/ipc, not the kernel router.
#
# requires:
#   drivers: []
#   skills: []
"""

PAI_PROMPT_MD_TEMPLATE = """\
# {name}

Role prompt for the {name} PAI.
"""

SUBAGENT_PROMPT_MD_TEMPLATE = """\
# {name}

Role prompt for the {name} persistent subagent. You are a long-lived
specialist child of your parent PAI. Describe the steady-state behavior
the parent should expect from you here.
"""


BUNDLE_TYPES = {
    "pai": (paths.usr_lib_pais, PAI_PACKAGE_YAML_TEMPLATE, PAI_PROMPT_MD_TEMPLATE),
    "subagent": (
        paths.usr_lib_subagents,
        SUBAGENT_PACKAGE_YAML_TEMPLATE,
        SUBAGENT_PROMPT_MD_TEMPLATE,
    ),
}


def _validate_name(name: str) -> None:
    # Mirrors src/boot/config.py:_validate_pai_entry name rules so a
    # scaffolded bundle is loadable by the config resolver.
    if not name:
        raise SystemExit("paiman: name must be non-empty")
    if "/" in name or name.startswith("."):
        raise SystemExit(f"paiman: invalid name {name!r}")


def cmd_init(args: argparse.Namespace) -> int:
    name: str = args.name
    bundle_type: str = args.type
    _validate_name(name)
    if bundle_type not in BUNDLE_TYPES:
        raise SystemExit(
            f"paiman: unknown --type {bundle_type!r} "
            f"(known: {', '.join(sorted(BUNDLE_TYPES))})"
        )
    root_fn, pkg_tmpl, prompt_tmpl = BUNDLE_TYPES[bundle_type]
    bundle_dir: Path = root_fn() / name
    if bundle_dir.exists():
        raise SystemExit(f"paiman: bundle already exists at {bundle_dir}")
    bundle_dir.mkdir(parents=True)
    (bundle_dir / "package.yaml").write_text(pkg_tmpl.format(name=name))
    (bundle_dir / "prompt.md").write_text(prompt_tmpl.format(name=name))
    print(f"scaffolded {bundle_type} bundle at {bundle_dir}")
    return 0


def _iter_bundles(bundle_type: str) -> list[tuple[str, dict]]:
    root_fn, _, _ = BUNDLE_TYPES[bundle_type]
    root = root_fn()
    if not root.exists():
        return []
    out: list[tuple[str, dict]] = []
    for entry in sorted(root.iterdir()):
        pkg = entry / "package.yaml"
        if not pkg.exists():
            continue
        try:
            with pkg.open() as f:
                data = yaml.safe_load(f) or {}
        except yaml.YAMLError as e:
            data = {"_error": str(e)}
        out.append((entry.name, data))
    return out


def cmd_list(args: argparse.Namespace) -> int:
    for bundle_type in ("pai", "subagent"):
        bundles = _iter_bundles(bundle_type)
        print(f"{bundle_type}s:")
        if not bundles:
            print("  (none)")
            continue
        for name, data in bundles:
            if "_error" in data:
                print(f"  {name}  [parse error: {data['_error']}]")
                continue
            desc = (data.get("description") or "").strip() or "(no description)"
            provider = data.get("provider") or "?"
            model = data.get("model")
            tail = f"{provider}" + (f"/{model}" if model else "")
            print(f"  {name}  [{tail}]  {desc}")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    name: str = args.name
    for bundle_type in ("pai", "subagent"):
        root_fn, _, _ = BUNDLE_TYPES[bundle_type]
        pkg = root_fn() / name / "package.yaml"
        if pkg.exists():
            print(f"# {pkg}")
            print(pkg.read_text(), end="")
            return 0
    raise SystemExit(f"paiman: bundle {name!r} not found")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="paiman", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="scaffold a new bundle template")
    p_init.add_argument("name", help="bundle name (e.g., email-pai)")
    p_init.add_argument(
        "--type",
        default="pai",
        choices=sorted(BUNDLE_TYPES),
        help="bundle type (default: pai)",
    )
    p_init.set_defaults(func=cmd_init)

    p_list = sub.add_parser("list", help="list installed bundles")
    p_list.set_defaults(func=cmd_list)

    p_show = sub.add_parser("show", help="print resolved package.yaml")
    p_show.add_argument("name", help="bundle name")
    p_show.set_defaults(func=cmd_show)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
