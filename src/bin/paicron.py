#!/usr/bin/env python
"""paicron — systemctl-shaped control for PAI services.

Talks to the running kernel only through the filesystem: writes spec files,
flips status, reads log tails. No IPC, no socket. The kernel's own watchers
pick up changes and act on them.

Manages services (cron jobs, watchers, one-shot async work). For PAI
instance lifecycle (start/stop a configured fleet member), see paictl.

Usage summary (see `paicron <cmd> --help` for specifics):

    paicron start --slug NAME --run 'CMD' [--restart POLICY] [--deadline ISO]
    paicron start --slug NAME --schedule 'EXPR' [--run 'CMD']
    paicron start --slug NAME --spec path/to/spec.yaml
    paicron stop     SLUG
    paicron restart  SLUG
    paicron status   SLUG
    paicron ls       [--status STATUS]
    paicron logs     SLUG [-f]
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import sys
import time
from pathlib import Path

import yaml

from boot import processes as P


DATE_SUFFIX = re.compile(r"-\d{4}-\d{2}-\d{2}(?:T\d{2}-\d{2}-\d{2})?$")


def _today_slug_suffix() -> str:
    return dt.date.today().isoformat()


def _full_slug_suffix() -> str:
    return dt.datetime.now().strftime("%Y-%m-%dT%H-%M-%S")


def _allocate_slug(base: str) -> str:
    """Append today's date; fall back to full timestamp if that collides."""
    candidate = f"{base}-{_today_slug_suffix()}"
    if not (P.PROC_DIR / candidate).exists():
        return candidate
    return f"{base}-{_full_slug_suffix()}"


def _base_from_slug(slug: str) -> str:
    return DATE_SUFFIX.sub("", slug)


def _build_spec_from_args(args: argparse.Namespace) -> dict:
    if args.spec:
        text = Path(args.spec).read_text()
        data = yaml.safe_load(text)
        if not isinstance(data, dict):
            raise ValueError(f"{args.spec} must contain a YAML object")
        return data

    spec: dict = {}
    if args.run:
        spec["run"] = args.run
    if args.schedule:
        spec["schedule"] = args.schedule
    if args.restart:
        spec["restart"] = args.restart
    if args.deadline:
        spec["deadline"] = args.deadline
    if args.description:
        spec["description"] = args.description
    if args.people:
        spec["people"] = [p.strip() for p in args.people.split(",") if p.strip()]
    if args.parent is not None:
        spec["parent"] = int(args.parent)
    return spec


def cmd_start(args: argparse.Namespace) -> int:
    if not args.slug:
        print("error: --slug is required", file=sys.stderr)
        return 1
    try:
        spec = _build_spec_from_args(args)
    except (OSError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    if "run" not in spec and "schedule" not in spec:
        print("error: spec must have `run:` or `schedule:`", file=sys.stderr)
        return 1

    slug = _allocate_slug(args.slug)
    try:
        P.spawn(slug, spec)
    except P.ProcessExists as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    # Stdout is the full slug, nothing else — pipeable.
    print(slug)
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    try:
        P.resolve(args.slug, "cancelled")
    except P.ProcessNotFound:
        print(f"error: no service {args.slug!r}", file=sys.stderr)
        return 1
    print(f"{args.slug} -> cancelled")
    return 0


def cmd_restart(args: argparse.Namespace) -> int:
    try:
        spec = P.read_spec(args.slug)
    except P.ProcessNotFound:
        print(f"error: no service {args.slug!r}", file=sys.stderr)
        return 1
    try:
        P.resolve(args.slug, "cancelled")
    except P.ProcessNotFound:
        pass
    spec = {k: v for k, v in spec.items() if k != "spawned"}
    new_slug = _allocate_slug(_base_from_slug(args.slug))
    P.spawn(new_slug, spec)
    print(new_slug)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    try:
        info = P.show(args.slug)
    except P.ProcessNotFound:
        print(f"error: no service {args.slug!r}", file=sys.stderr)
        return 1
    print(f"slug:   {info['slug']}")
    print(f"status: {info['status']}")
    print("spec:")
    print(yaml.safe_dump(info["spec"], sort_keys=False).rstrip())
    print("log (tail):")
    for line in info["log"].splitlines()[-20:]:
        print(line)
    return 0


def cmd_ls(args: argparse.Namespace) -> int:
    slugs = P.list_procs(status_filter=args.status)
    if not slugs:
        print("(no services)")
        return 0
    width = max(len(s) for s in slugs)
    for slug in slugs:
        try:
            status = P.read_status(slug)
            spec = P.read_spec(slug)
        except P.ProcessNotFound:
            continue
        summary = spec.get("description") or spec.get("run") or spec.get("schedule", "")
        print(f"{slug:<{width}}  {status:<10}  {summary}")
    return 0


def cmd_logs(args: argparse.Namespace) -> int:
    log_path = P.PROC_DIR / args.slug / "log.md"
    if not log_path.exists():
        print(f"error: no log for {args.slug!r}", file=sys.stderr)
        return 1
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
    parser = argparse.ArgumentParser(prog="paicron", description="Control PAI services.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("start", help="spawn a service")
    sp.add_argument("--slug", help="base slug (date is auto-appended)")
    sp.add_argument("--run", help="command to run (background service, or per-fire for cron)")
    sp.add_argument("--schedule", help="cron expression (recurring) or ISO datetime (one-shot)")
    sp.add_argument("--restart", choices=["never", "on-failure", "always"])
    sp.add_argument("--deadline", help="ISO datetime; kernel auto-expires if passed")
    sp.add_argument("--description", help="free-text description")
    sp.add_argument("--people", help="comma-separated list of related people")
    sp.add_argument("--parent", type=int, default=1, help="PID of the owning PAI (default: 1)")
    sp.add_argument("--spec", help="YAML file with the spec body (mutually exclusive with per-field flags)")
    sp.set_defaults(func=cmd_start)

    st = sub.add_parser("stop", help="cancel a running service")
    st.add_argument("slug")
    st.set_defaults(func=cmd_stop)

    rs = sub.add_parser("restart", help="cancel, then re-spawn with the same spec")
    rs.add_argument("slug")
    rs.set_defaults(func=cmd_restart)

    sh = sub.add_parser("status", help="show a service's spec, status, and log tail")
    sh.add_argument("slug")
    sh.set_defaults(func=cmd_status)

    ls = sub.add_parser("ls", help="list services")
    ls.add_argument("--status", help="filter by status")
    ls.set_defaults(func=cmd_ls)

    lg = sub.add_parser("logs", help="print (or tail with -f) a service's log.md")
    lg.add_argument("slug")
    lg.add_argument("-f", "--follow", action="store_true", help="follow the log")
    lg.set_defaults(func=cmd_logs)

    args = parser.parse_args(argv)
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
