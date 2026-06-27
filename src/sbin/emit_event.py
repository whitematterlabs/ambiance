#!/usr/bin/env python
"""emit-event — safely emit a kernel-bus event from a shell subprocess.

Shell has no structured-data type, so a watcher's only native move is to
hand-build YAML with `printf` and interpolate scraped strings. Scraped
values routinely contain `:`, quotes, or newlines → invalid YAML that the
kernel must quarantine (and the event is lost). This CLI lets a pid-less
subprocess (a cron poll, which inherits `$PAI_ROOT` but **not** `$PAI_PID`)
emit an event through the one sanctioned writer — `processes.emit_event`,
which `yaml.safe_dump`s the payload and atomically `os.replace`s it into
the bus. Payload comes in as explicit argv, so colons/quotes/newlines in a
value are a non-issue: the dict is built in Python and serialized safely.

Emitting onto the bus injects into the kernel's event stream and wakes
PAIs — a system-state mutation, so this is an sbin tool. It needs no
`$PAI_PID`; `processes.emit_event` resolves `$PAI_ROOT` via `boot.paths`.
"""

from __future__ import annotations

import argparse
import sys

from boot import processes as P


def _parse_set(pairs: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or not key:
            raise argparse.ArgumentTypeError(
                f"--set expects key=value, got {pair!r}"
            )
        out[key] = value
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="emit-event",
        description="Safely emit a kernel-bus event from a shell subprocess.",
    )
    parser.add_argument(
        "--source",
        required=True,
        help="event source slug (your watcher's slug; not 'kernel')",
    )
    parser.add_argument(
        "--kind",
        required=True,
        help="event kind, a bare word (becomes <source>:<kind> on the bus)",
    )
    parser.add_argument(
        "--target",
        type=int,
        default=None,
        help="deliver point-to-point to this pid (bypasses wake_on)",
    )
    parser.add_argument(
        "--note",
        default=None,
        help="convenience field → payload['note']",
    )
    parser.add_argument(
        "--set",
        dest="set_pairs",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="arbitrary string field (repeatable), e.g. --set url=https://…",
    )
    args = parser.parse_args(argv)

    source = args.source.strip()
    if not source:
        parser.error("--source must not be empty")
    if source == "kernel":
        parser.error(
            "--source must not be 'kernel' — that route drops extra keys; "
            "use your watcher's slug"
        )

    kind = args.kind.strip()
    if not kind:
        parser.error("--kind must not be empty")

    try:
        extra = _parse_set(args.set_pairs)
    except argparse.ArgumentTypeError as exc:
        parser.error(str(exc))

    payload: dict = {"source": source, "kind": kind}
    if args.note is not None:
        payload["note"] = args.note
    payload.update(extra)

    path = P.emit_event(payload, target_pid=args.target)
    print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
