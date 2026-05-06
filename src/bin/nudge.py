#!/usr/bin/env python
"""nudge — send a peer-to-peer nudge to another running PAI."""

from __future__ import annotations

import argparse
import os
import sys

from boot import processes as P


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="nudge",
        description="Send a peer-to-peer message to another PAI by PID.",
    )
    parser.add_argument("--to", type=int, required=True, help="target PAI pid")
    parser.add_argument("--content", required=True, help="message text")
    args = parser.parse_args(argv)

    sender_raw = os.environ.get("PAI_PID")
    if not sender_raw:
        print(
            "error: $PAI_PID not set — nudge must be invoked from a PAI turn",
            file=sys.stderr,
        )
        return 1
    try:
        sender_pid = int(sender_raw)
    except ValueError:
        print(f"error: $PAI_PID={sender_raw!r} is not an int", file=sys.stderr)
        return 1
    if args.to == sender_pid:
        print("error: cannot nudge yourself", file=sys.stderr)
        return 1

    P.emit_event({
        "source": "nudge",
        "kind": "pai_message",
        "target_pid": args.to,
        "sender_pid": sender_pid,
        "text": args.content,
    })
    print(f"nudged pid={args.to}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
