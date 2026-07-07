#!/usr/bin/env python
"""send-message — send a peer pai_message to one or more running PAIs.

Each send carries a unique msg_id. The kernel writes a delivery ack
(pai_message:ack on success, pai_message:dropped if the target pid is
gone) to /run/pai/acks/<msg_id>.yaml. send-message blocks on that file
for a short timeout so senders get verifiable delivery instead of
fire-and-forget.

If the target is mid-turn, the kernel injects the message into the
running turn at its next tool boundary (ack carries delivery: injected)
— the target sees it within one model/tool step and keeps working. An
ack timeout therefore means "queued, not yet picked up", not "lost":
the kernel still holds the message and delivers it when the target
frees up. Never resend on timeout."""

from __future__ import annotations

import argparse
import os
import sys
import time
import uuid

import yaml

from boot import processes as P
from boot.paths import ACKS_DIR


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


def _wait_for_ack(msg_id: str, timeout: float) -> dict | None:
    """Poll ACKS_DIR for the ack file. Returns parsed payload, or None on timeout."""
    path = ACKS_DIR / f"{msg_id}.yaml"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            try:
                with path.open() as f:
                    data = yaml.safe_load(f) or {}
            except (FileNotFoundError, yaml.YAMLError):
                data = None
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            return data if isinstance(data, dict) else None
        time.sleep(0.05)
    return None


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
    parser.add_argument(
        "--timeout",
        type=float,
        default=2.0,
        help="seconds to wait for kernel delivery ack per target (default: 2.0)",
    )
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

    failures = 0
    for pid in targets:
        msg_id = uuid.uuid4().hex
        P.emit_event({
            "source": "send-message",
            "kind": "pai_message",
            "target_pid": pid,
            "sender_pid": sender_pid,
            "text": args.content,
            "msg_id": msg_id,
        })
        ack = _wait_for_ack(msg_id, args.timeout)
        if ack is None:
            # Not a failure: the kernel holds the message and delivers it at
            # the target's next turn (or injects if a turn starts). Resending
            # would double-deliver.
            print(
                f"queued for pid={pid} (no ack within {args.timeout}s — "
                "the kernel will deliver it when the target picks it up; "
                "do NOT resend)"
            )
            continue
        kind = ack.get("kind")
        if kind == "pai_message:ack":
            how = (
                " — injected into its running turn"
                if ack.get("delivery") == "injected"
                else ""
            )
            print(f"delivered to pid={pid} (slug={ack.get('slug')}){how}")
        else:
            reason = ack.get("reason") or kind or "unknown"
            print(
                f"error: no PAI with pid={pid} ({reason})",
                file=sys.stderr,
            )
            failures += 1
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
