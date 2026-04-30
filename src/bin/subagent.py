#!/usr/bin/env python
"""subagent — spawn and manage subordinate PAI instances.

Usage:
    subagent spawn --slug NAME --prompt "..."   # fork a subagent, return its pid
    subagent reply --content "..."              # (child only) reply to your parent
    subagent done --slug NAME                   # resolve a subagent (parent or child)

Subagents are persistent: they stay alive across turns and do not
auto-resolve after answering the initial prompt. The kickoff prompt is
just a normal `pai_message` IPC — same channel used for parent→child
follow-ups. Children talk back to the parent via `subagent reply`,
which emits `subagent:response` events so the parent can distinguish
"one of my own children is talking" from a generic peer message.

Parents do NOT need to instruct the subagent on how to reply or how to
finish — every spawned subagent automatically gets a subagent-mode block
in its system prompt that explains the lifecycle. So `--prompt` should
just describe the task.

Either side can call `subagent done` to end the relationship: the parent
to dismiss the child, or the child to self-resolve once its task is
complete. Either path resolves the child and nudges the parent with the
final transcript pointer.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import sys

from boot import processes as P


DATE_SUFFIX = re.compile(r"-\d{4}-\d{2}-\d{2}(?:T\d{2}-\d{2}-\d{2})?$")

DEFAULT_MODEL = "deepseek/deepseek-v4-pro"


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

    provider, _, model = args.model.partition("/")
    if not provider or not model:
        print(f"error: --model must be 'provider/model-tag' (got {args.model!r})", file=sys.stderr)
        return 1

    final_slug = _allocate_slug(args.slug)
    child_pid = P.alloc_pai_pid()
    spec = {
        "kind": "pai",
        "pid": child_pid,
        "parent": parent_pid,
        "persistent": True,
        "description": args.prompt[:80],
        "provider": provider,
        "model": model,
    }
    try:
        P.spawn(final_slug, spec)
    except P.ProcessExists as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    # Kickoff is just the parent's first IPC to the newborn child — same
    # event kind any peer would use. The subagent itself talks back via
    # `bin/subagent reply`, which uses subagent:response so the parent can
    # tell at a glance that the message is from one of its own children.
    P.emit_event({
        "source": "subagent",
        "kind": "pai_message",
        "target_pid": child_pid,
        "sender_pid": parent_pid,
        "text": args.prompt,
    })

    print(f"{final_slug} (pid {child_pid})")
    return 0


def cmd_reply(args: argparse.Namespace) -> int:
    sender_raw = os.environ.get("PAI_PID")
    parent_raw = os.environ.get("PAI_PARENT")
    if not sender_raw:
        print("error: $PAI_PID not set — reply must be invoked from a PAI turn", file=sys.stderr)
        return 1
    if not parent_raw:
        print("error: $PAI_PARENT not set — only subagents can reply", file=sys.stderr)
        return 1
    try:
        sender_pid = int(sender_raw)
        parent_pid = int(parent_raw)
    except ValueError:
        print("error: $PAI_PID/$PAI_PARENT must be ints", file=sys.stderr)
        return 1

    P.emit_event({
        "source": "subagent",
        "kind": "subagent:response",
        "target_pid": parent_pid,
        "sender_pid": sender_pid,
        "text": args.content,
    })
    print(f"replied to parent pid={parent_pid}")
    return 0


def cmd_done(args: argparse.Namespace) -> int:
    parent_pid_raw = os.environ.get("PAI_PID")
    if not parent_pid_raw:
        print("error: $PAI_PID not set — subagent must be invoked from a PAI turn", file=sys.stderr)
        return 1
    try:
        parent_pid = int(parent_pid_raw)
    except ValueError:
        print(f"error: $PAI_PID={parent_pid_raw!r} is not an int", file=sys.stderr)
        return 1

    try:
        spec = P.read_spec(args.slug)
    except P.ProcessNotFound:
        print(f"error: no proc named {args.slug!r}", file=sys.stderr)
        return 1
    if spec.get("kind") != "pai" or "parent" not in spec:
        print(f"error: {args.slug!r} is not a subagent", file=sys.stderr)
        return 1
    if parent_pid != int(spec["parent"]) and parent_pid != int(spec["pid"]):
        print(
            f"error: {args.slug!r} can only be resolved by its parent (pid {spec['parent']}) "
            f"or itself (pid {spec['pid']}); you are pid {parent_pid}",
            file=sys.stderr,
        )
        return 1

    try:
        P.resolve(args.slug, "completed")
    except P.ProcessNotFound:
        print(f"error: {args.slug!r} disappeared", file=sys.stderr)
        return 1
    print(f"{args.slug} resolved")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="subagent",
        description=(
            "Spawn and manage PAI subagents. Subagents are persistent — they "
            "stay alive across turns. The kickoff --prompt is the task itself; "
            "you do NOT need to explain how to reply or self-resolve, the "
            "subagent already knows its own lifecycle (it gets a subagent-mode "
            "block in its system prompt). Just describe the work."
        ),
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser(
        "spawn",
        help="spawn a persistent subagent",
        description=(
            "Spawn a subagent. --prompt should describe the task only — the "
            "subagent already knows to reply via `bin/subagent reply` and to "
            "self-resolve via `bin/subagent done` when finished, so you don't "
            "need to spell that out. Either side can call `done` to end the "
            "relationship."
        ),
    )
    sp.add_argument("--slug", required=True, help="base slug (date is auto-appended)")
    sp.add_argument(
        "--prompt",
        required=True,
        help="task for the subagent (just the work — lifecycle is auto-injected)",
    )
    sp.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"provider/model-tag (default: {DEFAULT_MODEL})",
    )
    sp.set_defaults(func=cmd_spawn)

    rp = sub.add_parser("reply", help="(child only) send a subagent:response to your parent")
    rp.add_argument("--content", required=True, help="message text")
    rp.set_defaults(func=cmd_reply)

    dn = sub.add_parser(
        "done",
        help="resolve a subagent (callable by the parent OR the subagent itself)",
    )
    dn.add_argument("--slug", required=True, help="full slug as printed by spawn (or $PAI_SLUG if self-resolving)")
    dn.set_defaults(func=cmd_done)

    args = parser.parse_args(argv)
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
