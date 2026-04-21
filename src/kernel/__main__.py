"""CLI for the PAI kernel: spawn, ls, show, resolve, run, event."""

import argparse
import asyncio
import json
import sys

import yaml

from . import processes as P
from . import main as kernel_main


def cmd_spawn(args: argparse.Namespace) -> int:
    spec: dict = {"type": args.type}
    if args.deadline:
        spec["deadline"] = args.deadline
    if args.schedule:
        spec["schedule"] = args.schedule
    if args.description:
        spec["description"] = args.description
    if args.people:
        spec["people"] = [p.strip() for p in args.people.split(",") if p.strip()]
    if args.resolve_on:
        spec["resolve_on"] = args.resolve_on
    if args.depends_on:
        spec["depends_on"] = args.depends_on

    try:
        path = P.spawn(args.slug, spec)
    except P.ProcessExists as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"spawned {args.slug} at {path}")
    return 0


def cmd_ls(args: argparse.Namespace) -> int:
    slugs = P.list_procs(status_filter=args.status)
    if not slugs:
        print("(no processes)")
        return 0
    width = max(len(s) for s in slugs)
    for slug in slugs:
        status = P.read_status(slug)
        spec = P.read_spec(slug)
        desc = spec.get("description", "")
        print(f"{slug:<{width}}  {status:<10}  {desc}")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    try:
        info = P.show(args.slug)
    except P.ProcessNotFound:
        print(f"error: no process {args.slug!r}", file=sys.stderr)
        return 1
    print(f"# {info['slug']}")
    print(f"status: {info['status']}")
    print("spec:")
    print(yaml.safe_dump(info["spec"], sort_keys=False).rstrip())
    print("log:")
    print(info["log"].rstrip())
    return 0


def cmd_resolve(args: argparse.Namespace) -> int:
    try:
        P.resolve(args.slug, args.status)
    except P.ProcessNotFound:
        print(f"error: no process {args.slug!r}", file=sys.stderr)
        return 1
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"{args.slug} -> {args.status}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    try:
        asyncio.run(kernel_main.run())
    except KeyboardInterrupt:
        pass
    return 0


def cmd_event(args: argparse.Namespace) -> int:
    payload: dict = {"source": args.source, "kind": args.kind}
    if args.json:
        try:
            extra = json.loads(args.json)
        except json.JSONDecodeError as e:
            print(f"error: --json is not valid JSON: {e}", file=sys.stderr)
            return 1
        if not isinstance(extra, dict):
            print("error: --json must be an object", file=sys.stderr)
            return 1
        payload.update(extra)
    path = P.emit_event(payload)
    print(f"emitted {path.name}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kernel")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("spawn", help="spawn a new process")
    sp.add_argument("--slug", required=True)
    sp.add_argument(
        "--type",
        required=True,
        choices=["plan", "follow-up", "reminder", "cron", "subagent"],
    )
    sp.add_argument("--deadline")
    sp.add_argument("--schedule")
    sp.add_argument("--description")
    sp.add_argument("--people", help="comma-separated list")
    sp.add_argument(
        "--resolve-on",
        choices=["deadline", "confirmation", "dependency", "completion", "schedule"],
    )
    sp.add_argument("--depends-on")
    sp.set_defaults(func=cmd_spawn)

    ls = sub.add_parser("ls", help="list processes")
    ls.add_argument("--status", help="filter by status")
    ls.set_defaults(func=cmd_ls)

    sh = sub.add_parser("show", help="show a process")
    sh.add_argument("slug")
    sh.set_defaults(func=cmd_show)

    rv = sub.add_parser("resolve", help="resolve a process")
    rv.add_argument("slug")
    rv.add_argument("status", choices=sorted(P.VALID_STATUSES))
    rv.set_defaults(func=cmd_resolve)

    rn = sub.add_parser("run", help="run the kernel loop (foreground)")
    rn.set_defaults(func=cmd_run)

    ev = sub.add_parser("event", help="manually emit an event file")
    ev.add_argument("--source", required=True)
    ev.add_argument("--kind", required=True)
    ev.add_argument("--json", help="extra fields as a JSON object")
    ev.set_defaults(func=cmd_event)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
