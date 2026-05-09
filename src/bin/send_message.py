#!/usr/bin/env python
"""send-message — send a peer pai_message to one or more running PAIs."""

from __future__ import annotations

import argparse
import os
import sys

from boot import processes as P


def _resolve_targets(to_args: list[str], sender_pid: int) -> list[int]:
    """Expand --to values (ints, comma-lists, 'all') into a deduped pid list,
    excluding the sender."""
    pids: list[int] = []
    seen: set[int] = set()

    def add(pid: int) -> None:
        if pid == sender_pid or pid in seen:
            return
        seen.add(pid)
        pids.append(pid)

    for raw in to_args:
        for token in raw.split(","):
            token = token.strip()
            if not token:
                continue
            if token.lower() == "all":
                for _slug, spec in P._iter_pai_specs():
                    pid = spec.get("pid")
                    if isinstance(pid, int):
                        add(pid)
                continue
            try:
                add(int(token))
            except ValueError:
                raise SystemExit(f"error: --to value {token!r} is not an int or 'all'")
    return pids


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="send-message",
        description="Send a peer-to-peer message to one or more PAIs by PID.",
    )
    parser.add_argument(
        "--to",
        action="append",
        required=True,
        help="target PAI pid; repeatable, comma-separated, or 'all' for every running PAI except self",
    )
    parser.add_argument("--content", required=True, help="message text")
    args = parser.parse_args(argv)

    sender_raw = os.environ.get("PAI_PID")
    if not sender_raw:
        print(
            "error: $PAI_PID not set — send-message must be invoked from a PAI turn",
            file=sys.stderr,
        )
        return 1
    try:
        sender_pid = int(sender_raw)
    except ValueError:
        print(f"error: $PAI_PID={sender_raw!r} is not an int", file=sys.stderr)
        return 1

    targets = _resolve_targets(args.to, sender_pid)
    if not targets:
        print("error: no valid targets (after excluding self)", file=sys.stderr)
        return 1

    for pid in targets:
        P.emit_event({
            "source": "send-message",
            "kind": "pai_message",
            "target_pid": pid,
            "sender_pid": sender_pid,
            "text": args.content,
        })
        print(f"sent to pid={pid}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
