#!/usr/bin/env python
"""subagent — spawn and manage subordinate PAI instances.

Usage:
    subagent spawn --slug NAME --prompt "..."   # fork a subagent, return its pid
    subagent reply --content "..."              # (child only) reply to your parent
    subagent kill --slug NAME                   # resolve a subagent (parent or child)

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

Either side can call `subagent kill` to end the relationship: the parent
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

from boot import config as C
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
    if not args.persistent and not args.prompt:
        print("error: --prompt is required (omit only with --persistent)", file=sys.stderr)
        return 1
    bundle: dict = {}
    if args.package:
        try:
            bundle = C.resolve_subagent_package(args.package)
        except C.ConfigError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1

    # Resolution: explicit --model wins; else bundle's provider/model;
    # else DEFAULT_MODEL.
    if args.model:
        model_str = args.model
    elif bundle.get("provider") and bundle.get("model"):
        model_str = f"{bundle['provider']}/{bundle['model']}"
    else:
        model_str = DEFAULT_MODEL
    provider, _, model = model_str.partition("/")
    if not provider or not model:
        print(f"error: --model must be 'provider/model-tag' (got {model_str!r})", file=sys.stderr)
        return 1

    if args.persistent:
        # Persubs are deterministic singletons under their parent: no date
        # suffix, namespaced under the parent's slug so two parents can each
        # have a `memory` child without colliding.
        parent_slug = os.environ.get("PAI_SLUG")
        if not parent_slug:
            print("error: $PAI_SLUG not set — required for --persistent", file=sys.stderr)
            return 1
        final_slug = f"{parent_slug}.{args.slug}"
    else:
        final_slug = _allocate_slug(args.slug)

    child_pid = P.alloc_pai_pid()
    description = (
        args.prompt
        or (bundle.get("description") if bundle else None)
        or f"persub: {args.slug}"
    )[:80]
    spec = {
        "kind": "pai",
        "pid": child_pid,
        "parent": parent_pid,
        "persistent": True,
        "description": description,
        "provider": provider,
        "model": model,
    }
    if args.persistent:
        spec["persub"] = True
    if bundle.get("prompt"):
        spec["prompt"] = bundle["prompt"]
    if bundle.get("debugger"):
        spec["debugger"] = bundle["debugger"]
    try:
        P.spawn(final_slug, spec)
    except P.ProcessExists as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    if not args.persistent:
        # Kickoff is just the parent's first IPC to the newborn child — same
        # event kind any peer would use. Persubs have no kickoff: they boot
        # idle and wait for the parent to message them.
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


def _read_sender_pid() -> int | None:
    raw = os.environ.get("PAI_PID")
    if not raw:
        print("error: $PAI_PID not set — must be invoked from a PAI turn", file=sys.stderr)
        return None
    try:
        return int(raw)
    except ValueError:
        print(f"error: $PAI_PID={raw!r} is not an int", file=sys.stderr)
        return None


def cmd_plan_ready(args: argparse.Namespace) -> int:
    sender_pid = _read_sender_pid()
    if sender_pid is None:
        return 1
    parent_raw = os.environ.get("PAI_PARENT")
    if not parent_raw:
        print("error: $PAI_PARENT not set — only subagents can declare plan-ready", file=sys.stderr)
        return 1
    try:
        parent_pid = int(parent_raw)
    except ValueError:
        print(f"error: $PAI_PARENT={parent_raw!r} is not an int", file=sys.stderr)
        return 1
    P.emit_event({
        "source": "subagent",
        "kind": "subagent:plan_ready",
        "target_pid": parent_pid,
        "sender_pid": sender_pid,
        "slug": os.environ.get("PAI_SLUG", ""),
        "text": args.content or "",
    })
    print(f"emitted subagent:plan_ready → pid={parent_pid}")
    return 0


def cmd_plan_reject(args: argparse.Namespace) -> int:
    sender_pid = _read_sender_pid()
    if sender_pid is None:
        return 1
    try:
        spec = P.read_spec(args.slug)
    except P.ProcessNotFound:
        print(f"error: no subagent {args.slug!r}", file=sys.stderr)
        return 1
    target_pid = spec.get("pid")
    if target_pid is None:
        print(f"error: {args.slug!r} has no pid", file=sys.stderr)
        return 1
    P.emit_event({
        "source": "subagent",
        "kind": "subagent:plan_reject",
        "target_pid": int(target_pid),
        "sender_pid": sender_pid,
        "slug": args.slug,
        "text": args.content or "",
    })
    print(f"emitted subagent:plan_reject → {args.slug} (pid={target_pid})")
    return 0


def cmd_kill(args: argparse.Namespace) -> int:
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
    if spec.get("persub"):
        print(
            f"error: {args.slug!r} is a persistent subagent and cannot be killed; "
            f"remove it from /etc/config.yaml `dependencies:` and reload",
            file=sys.stderr,
        )
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
            "self-resolve via `bin/subagent kill` when finished, so you don't "
            "need to spell that out. Either side can call `kill` to end the "
            "relationship."
        ),
    )
    sp.add_argument("--slug", required=True, help="base slug (date is auto-appended unless --persistent)")
    sp.add_argument(
        "--prompt",
        help="task for the subagent (required for ephemeral; optional for --persistent)",
    )
    sp.add_argument(
        "--model",
        default=None,
        help=f"provider/model-tag (overrides --package; default: {DEFAULT_MODEL})",
    )
    sp.add_argument(
        "--persistent",
        action="store_true",
        help=(
            "spawn as a persub (persistent subagent): deterministic slug "
            "<parent>.<name>, no kickoff prompt, cannot be resolved by `kill`"
        ),
    )
    sp.add_argument(
        "--package",
        default=None,
        help=(
            "(with --persistent) name of a /usr/lib/subagents/<name>/ bundle "
            "to pull prompt/provider/model from"
        ),
    )
    sp.set_defaults(func=cmd_spawn)

    rp = sub.add_parser("reply", help="(child only) send a subagent:response to your parent")
    rp.add_argument("--content", required=True, help="message text")
    rp.set_defaults(func=cmd_reply)

    pr = sub.add_parser(
        "plan-ready",
        help="(child only) declare /proc/$PAI_SLUG/plan.md ready; parent gets a 30s ack window (silence=approval)",
    )
    pr.add_argument("--content", default="", help="optional inline plan text")
    pr.set_defaults(func=cmd_plan_ready)

    pj = sub.add_parser(
        "plan-reject",
        help="(parent only) reject a subagent's plan; subagent revises before continuing",
    )
    pj.add_argument("--slug", required=True, help="child subagent slug to reject")
    pj.add_argument("--content", default="", help="rejection reason / correction")
    pj.set_defaults(func=cmd_plan_reject)

    dn = sub.add_parser(
        "kill",
        help="resolve a subagent (callable by the parent OR the subagent itself)",
    )
    dn.add_argument("--slug", required=True, help="full slug as printed by spawn (or $PAI_SLUG if self-resolving)")
    dn.set_defaults(func=cmd_kill)

    args = parser.parse_args(argv)
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
