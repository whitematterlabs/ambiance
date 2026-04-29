#!/usr/bin/env python
"""subagent — spawn another PAI instance owned by the current PAI.

Usage:
    subagent spawn --slug NAME --prompt "..."

Reads $PAI_SLUG from env (the caller's own slug). The kernel picks up the
`pai_kickoff` event, runs the subagent for one turn, auto-resolves it, and
nudges the parent with the transcript location.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import sys

from boot import processes as P


DATE_SUFFIX = re.compile(r"-\d{4}-\d{2}-\d{2}(?:T\d{2}-\d{2}-\d{2})?$")


def _today_slug_suffix() -> str:
    return dt.date.today().isoformat()


def _full_slug_suffix() -> str:
    return dt.datetime.now().strftime("%Y-%m-%dT%H-%M-%S")


def _allocate_slug(base: str) -> str:
    candidate = f"{base}-{_today_slug_suffix()}"
    if not (P.PROC_DIR / candidate).exists():
        return candidate
    return f"{base}-{_full_slug_suffix()}"


def cmd_spawn(args: argparse.Namespace) -> int:
    parent_pid_raw = os.environ.get("PAI_PID")
    if not parent_pid_raw:
        print("error: $PAI_PID not set — subagent must be invoked from a PAI turn", file=sys.stderr)
        return 1
    try:
        parent_pid = int(parent_pid_raw)
    except ValueError:
        print(f"error: $PAI_PID={parent_pid_raw!r} is not an int", file=sys.stderr)
        return 1
    if not args.slug:
        print("error: --slug is required", file=sys.stderr)
        return 1
    if not args.prompt:
        print("error: --prompt is required", file=sys.stderr)
        return 1

    final_slug = _allocate_slug(args.slug)
    child_pid = P.alloc_pai_pid()
    spec = {
        "kind": "pai",
        "pid": child_pid,
        "parent": parent_pid,
        "description": args.prompt[:80],
    }
    try:
        P.spawn(final_slug, spec)
    except P.ProcessExists as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    P.emit_event({
        "source": "subagent",
        "kind": "pai_kickoff",
        "target_pid": child_pid,
        "sender_pid": parent_pid,
        "text": args.prompt,
    })

    print(f"{final_slug} (pid {child_pid})")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="subagent", description="Spawn PAI subagents.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("spawn", help="spawn a one-shot subagent")
    sp.add_argument("--slug", required=True, help="base slug (date is auto-appended)")
    sp.add_argument("--prompt", required=True, help="instruction passed to the subagent")
    sp.set_defaults(func=cmd_spawn)

    args = parser.parse_args(argv)
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
