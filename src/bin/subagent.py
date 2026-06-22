#!/usr/bin/env python
"""subagent — spawn and manage subordinate PAI instances.

Usage:
    subagent spawn --slug NAME --prompt "..."             # fork a subagent, return its pid
    subagent reply --content "..."                        # (child only) intermediate update to parent
    subagent reply --done --content "..."                 # (child only) final reply; kernel reaps the child
    subagent done --result result.md                      # (child only) finish with durable parent-workspace result
    subagent kill --slug NAME                             # (parent only) abort a child you spawned

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

Standard exit: the subagent writes its report under
`$PAI_PARENT_HOME/workspace/$PAI_SLUG/`, then calls
`subagent done --result result.md`. The kernel emits a tiny completion
event pointing at the result, then resolves the proc — so the parent's
wake-up nudge already reflects a dead child and any out-of-band
`send-message` the parent attempts will fail loudly instead of racing a
self-kill. `subagent kill` is the parent's escape hatch for aborting a
child; it is not for self-termination.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
from pathlib import Path
import re
import sys

import yaml

from boot import config as C
from boot import paths as PATHS
from boot import processes as P
from boot import stitch as S


def _browse_orphan_tabs_block() -> str:
    """For browse subagents: list claimable orphan tabs as a kickoff prefix.
    Returns '' if none — caller skips the section."""
    tab_dir = PATHS.PAI_ROOT / "sys" / "drivers" / "browse" / "tabs"
    if not tab_dir.is_dir():
        return ""
    orphans: list[tuple[str, str, str]] = []  # (tab_id, title, age_iso)
    for tf in sorted(tab_dir.glob("*.yaml")):
        try:
            data = yaml.safe_load(tf.read_text()) or {}
        except Exception:
            continue
        if data.get("owner_status") != "orphan":
            continue
        tid = data.get("tab_id")
        if not tid:
            continue
        title = (data.get("last_title") or data.get("last_url") or "(untitled)")[:80]
        orphans.append((str(tid), title, str(data.get("created", ""))))
    if not orphans:
        return ""
    lines = ["AVAILABLE TABS (claim with `browse claim <tab_id>` or ignore for a fresh one):"]
    for tid, title, created in orphans:
        suffix = f"  ({created})" if created else ""
        lines.append(f"  - {tid}  \"{title}\"  (orphan){suffix}")
    return "\n".join(lines) + "\n\n"


DATE_SUFFIX = re.compile(r"-\d{4}-\d{2}-\d{2}(?:T\d{2}-\d{2}-\d{2})?$")

# Last-resort fallback only — used when neither the spawning PAI's spec nor
# the fleet default in config.yaml yield a provider/model. In practice the
# cascade in `_inherited_model` resolves first, so a subagent inherits the
# model the owner picked at install time rather than this hardcoded one.
DEFAULT_MODEL = "deepseek/deepseek-v4-pro"


def _fleet_default_model() -> tuple[str | None, str | None]:
    """The owner-chosen default provider/model, read from config.yaml.

    install.sh seeds the fleet's provider/model into config.yaml; this returns
    the default (`fallback:` PAI, else the reserved `pai` entry) so subagents
    track that choice instead of a hardcoded preset."""
    try:
        cfg = C.load_config()
    except (C.ConfigError, OSError):
        return None, None
    for spec in cfg.values():
        if spec.get("fallback"):
            return spec.get("provider"), spec.get("model")
    pai = cfg.get("pai")
    if pai:
        return pai.get("provider"), pai.get("model")
    return None, None


def _inherited_model(parent_pid: int) -> tuple[str, str]:
    """The provider/model a subagent should inherit when nothing pins it.

    Cascade: the spawning PAI's own spec (reconcile writes its provider/model
    from config.yaml) → the fleet default in config.yaml → DEFAULT_MODEL. The
    parent's spec is the direct answer to "run me on whatever my parent runs
    on", which is the owner's install-time choice for the top-level PAI."""
    spec: dict | None = None
    parent_slug = os.environ.get("PAI_SLUG")
    if parent_slug:
        try:
            spec = P.read_spec(parent_slug)
        except P.ProcessNotFound:
            spec = None
    if spec is None:
        try:
            spec = P.read_spec(P.find_pai_slug(parent_pid))
        except P.ProcessNotFound:
            spec = None
    if spec and spec.get("provider") and spec.get("model"):
        return spec["provider"], spec["model"]
    fp, fm = _fleet_default_model()
    if fp and fm:
        return fp, fm
    provider, _, model = DEFAULT_MODEL.partition("/")
    return provider, model


def _today_slug_suffix() -> str:
    return dt.date.today().isoformat()


def _full_slug_suffix() -> str:
    return dt.datetime.now().strftime("%Y-%m-%dT%H-%M-%S")


# A trailing date the caller already baked into the slug, in either ISO
# (2026-06-17) or compact (20260617) form. Used to avoid double-dating.
_DATE_TAIL = re.compile(r"-?\d{4}-?\d{2}-?\d{2}$")


def _allocate_slug(base: str) -> str:
    # If the caller already ended the slug with a date (e.g.
    # "market-check-20260617"), don't tack a second one on — that is what
    # produced double-dated slugs like "market-check-20260617-2026-06-17".
    candidate = base if _DATE_TAIL.search(base) else f"{base}-{_today_slug_suffix()}"
    if not (P.PROC_DIR / candidate).exists():
        return candidate
    # Collision: fall back to a full timestamp for guaranteed uniqueness.
    return f"{base}-{_full_slug_suffix()}"


def _installed_subagent_package_names() -> set[str]:
    """Installed subagent bundle names, best-effort for spawn guardrails."""
    root = C.SUBAGENTS_DIR
    if not root.is_dir():
        return set()
    names: set[str] = set()
    for entry in root.iterdir():
        if entry.name.startswith(".") or not entry.is_dir():
            continue
        if (entry / "package.yaml").is_file():
            names.add(entry.name)
    return names


def _package_hint_from_slug(slug: str, packages: set[str]) -> str | None:
    """Return the installed package name implied by a slug tail, if any.

    A parent can still choose any neutral slug for a generic subagent, but a
    slug like `sf-apt-search-browse` while `browse` is installed is almost
    certainly a missing `--package browse`.
    """
    for package in sorted(packages, key=len, reverse=True):
        if (
            slug.endswith(f"-{package}")
            or slug.endswith(f".{package}")
            or slug.endswith(f"_{package}")
        ):
            return package
    return None


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
    if not args.package:
        hinted_package = _package_hint_from_slug(
            args.slug,
            _installed_subagent_package_names(),
        )
        if hinted_package:
            print(
                f"error: slug {args.slug!r} looks like it names installed "
                f"subagent package {hinted_package!r}, but --package was "
                f"omitted. Use `--package {hinted_package}` or choose a "
                f"neutral slug for a generic subagent.",
                file=sys.stderr,
            )
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

    # Resolution per-field so a bundle can pin just one (e.g. scout pins
    # provider only). Each field cascades: explicit --model > bundle pin >
    # inherited (parent PAI spec → fleet default → DEFAULT_MODEL). A subagent
    # with no pins runs on the owner's install-time model, not a preset.
    if args.model:
        cli_provider, _, cli_model = args.model.partition("/")
        if not cli_provider or not cli_model:
            print(f"error: --model must be 'provider/model-tag' (got {args.model!r})", file=sys.stderr)
            return 1
    else:
        cli_provider = cli_model = ""
    inh_provider, inh_model = _inherited_model(parent_pid)
    provider = cli_provider or bundle.get("provider") or inh_provider
    model = cli_model or bundle.get("model") or inh_model
    if not provider or not model:
        print(f"error: could not resolve provider/model (got {provider!r}/{model!r})", file=sys.stderr)
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
    if args.package:
        spec["package"] = args.package
    if args.persistent:
        spec["persub"] = True
    if args.package and bundle.get("prompt"):
        spec["prompt"] = C._resolve_subagent_bundle_path(
            args.package,
            bundle["prompt"],
        )
    if args.package and bundle.get("prompt_dir"):
        spec["prompt_dir"] = C._resolve_subagent_bundle_path(
            args.package, bundle["prompt_dir"]
        )
    if bundle.get("debugger"):
        spec["debugger"] = bundle["debugger"]
    try:
        P.spawn(final_slug, spec)
    except P.ProcessExists as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    # Subagents spawned here would otherwise sit in an empty /home/<slug>/.
    # Reconcile handles fleet members; this is the equivalent for subagents.
    S.stitch_home(final_slug)

    if not args.persistent:
        # Kickoff is just the parent's first IPC to the newborn child — same
        # event kind any peer would use. Persubs have no kickoff: they boot
        # idle and wait for the parent to message them.
        kickoff_text = args.prompt
        if bundle.get("name") == "browse":
            prefix = _browse_orphan_tabs_block()
            if prefix:
                kickoff_text = prefix + "YOUR TASK:\n" + args.prompt
        P.emit_event({
            "source": "subagent",
            "kind": "pai_message",
            "target_pid": child_pid,
            "sender_pid": parent_pid,
            "text": kickoff_text,
        })

    if args.persistent:
        print(
            f"{final_slug} (pid {child_pid}) — persistent subagent, booted idle. "
            f"Message it with `bin/send-message --to {child_pid} --content ...`; "
            f"it replies as a 'subagent response' message you'll be woken for."
        )
    else:
        print(
            f"{final_slug} (pid {child_pid}) — running. Its result arrives as a "
            f"'subagent response' message that wakes you; end your turn and wait "
            f"for it. Do NOT poll /proc/{final_slug} — the child reaps its own "
            f"/proc the instant it finishes, so a poll loop just races the reap."
        )
    return 0


def _read_child_reply_env(
    command: str,
    *,
    require_slug: bool = False,
) -> tuple[int, int, str] | None:
    sender_raw = os.environ.get("PAI_PID")
    parent_raw = os.environ.get("PAI_PARENT")
    slug = os.environ.get("PAI_SLUG")
    if not sender_raw:
        print(f"error: $PAI_PID not set — {command} must be invoked from a PAI turn", file=sys.stderr)
        return None
    if not parent_raw:
        print(f"error: $PAI_PARENT not set — only subagents can use `{command}`", file=sys.stderr)
        return None
    if require_slug and not slug:
        print(f"error: $PAI_SLUG not set — required for `{command}`", file=sys.stderr)
        return None
    try:
        sender_pid = int(sender_raw)
        parent_pid = int(parent_raw)
    except ValueError:
        print("error: $PAI_PID/$PAI_PARENT must be ints", file=sys.stderr)
        return None
    return sender_pid, parent_pid, slug or ""


def _ensure_can_finish(slug: str) -> bool:
    try:
        own_spec = P.read_spec(slug)
    except P.ProcessNotFound:
        print(f"error: own proc {slug!r} not found", file=sys.stderr)
        return False
    if own_spec.get("persub"):
        print(
            f"error: {slug!r} is a persistent subagent and cannot finish itself; "
            f"reply with `bin/subagent reply --content ...` and wait for the parent",
            file=sys.stderr,
        )
        return False
    return True


def _emit_parent_response(
    *,
    sender_pid: int,
    parent_pid: int,
    text: str,
    done: bool = False,
    result: str | None = None,
) -> None:
    payload = {
        "source": "subagent",
        "kind": "subagent:response",
        "target_pid": parent_pid,
        "sender_pid": sender_pid,
        "text": text,
    }
    if done:
        payload["done"] = True
    if result:
        payload["result"] = result
    P.emit_event(payload)


def _resolve_done(slug: str) -> bool:
    # Emit-then-resolve ordering matters: the response event lands before the
    # proc_resolved event, so the parent's wake-up nudge sees the reply with a
    # now-dead child slug. The response above is the parent's notification, so
    # resolve quietly: notify_parent=False suppresses a redundant
    # "proc completed" nudge behind the response.
    try:
        P.resolve(slug, "completed", notify_parent=False)
    except P.ProcessNotFound:
        print(f"error: own proc {slug!r} not found", file=sys.stderr)
        return False
    return True


def _parent_home(parent_pid: int) -> Path | None:
    raw = os.environ.get("PAI_PARENT_HOME")
    if raw:
        return Path(raw)
    try:
        parent_slug = P.find_pai_slug(parent_pid)
    except P.ProcessNotFound:
        print(f"error: parent pid={parent_pid} not found", file=sys.stderr)
        return None
    return P.HOME_DIR / parent_slug


def _normalize_result_path(result: str, *, parent_pid: int, child_slug: str) -> str | None:
    parent_home = _parent_home(parent_pid)
    if parent_home is None:
        return None
    raw = Path(result)
    if raw.is_absolute():
        result_path = raw
    elif raw.parts and raw.parts[0] == "workspace":
        result_path = parent_home / raw
    else:
        result_path = parent_home / "workspace" / child_slug / raw

    parent_home_resolved = parent_home.resolve()
    workspace_resolved = (parent_home / "workspace" / child_slug).resolve()
    result_resolved = result_path.resolve()
    try:
        result_resolved.relative_to(workspace_resolved)
    except ValueError:
        print(
            "error: --result must point inside "
            f"$PAI_PARENT_HOME/workspace/$PAI_SLUG (got {result!r})",
            file=sys.stderr,
        )
        return None
    if not result_resolved.is_file():
        print(
            f"error: result file not found: {result_resolved}. "
            "Write the report before calling `subagent done`.",
            file=sys.stderr,
        )
        return None
    return result_resolved.relative_to(parent_home_resolved).as_posix()


def cmd_reply(args: argparse.Namespace) -> int:
    env = _read_child_reply_env("reply", require_slug=args.done)
    if env is None:
        return 1
    sender_pid, parent_pid, child_slug = env

    if args.done and not _ensure_can_finish(child_slug):
        return 1

    _emit_parent_response(
        sender_pid=sender_pid,
        parent_pid=parent_pid,
        text=args.content,
        done=args.done,
    )

    if args.done:
        if not _resolve_done(child_slug):
            return 1
        print(f"replied to parent pid={parent_pid} (done)")
        return 0

    print(f"replied to parent pid={parent_pid}")
    return 0


def cmd_done(args: argparse.Namespace) -> int:
    env = _read_child_reply_env("done", require_slug=True)
    if env is None:
        return 1
    sender_pid, parent_pid, child_slug = env
    if not _ensure_can_finish(child_slug):
        return 1
    result = _normalize_result_path(args.result, parent_pid=parent_pid, child_slug=child_slug)
    if result is None:
        return 1

    _emit_parent_response(
        sender_pid=sender_pid,
        parent_pid=parent_pid,
        text=f"done: {result}",
        done=True,
        result=result,
    )
    if not _resolve_done(child_slug):
        return 1
    print(f"replied to parent pid={parent_pid} (done: {result})")
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
    if parent_pid != int(spec["parent"]):
        print(
            f"error: {args.slug!r} can only be aborted by its parent (pid {spec['parent']}); "
            f"you are pid {parent_pid}. Subagents end via `subagent done --result`, not kill.",
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
            "you do NOT need to explain how to reply or how to finish — the "
            "subagent already knows its own lifecycle (it gets a subagent-mode "
            "block in its system prompt teaching `done --result result.md` as "
            "the standard exit). Just describe the work."
        ),
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser(
        "spawn",
        help="spawn a persistent subagent",
        description=(
            "Spawn a subagent. --prompt should describe the task only — the "
            "subagent already knows to send intermediate updates via "
            "`bin/subagent reply` and to finish via `bin/subagent done --result result.md`, "
            "so you don't need to spell that out. As the parent, you can call "
            "`bin/subagent kill` to abort a child early."
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
        help="provider/model-tag (overrides --package; default: inherit the spawning PAI's model / fleet default)",
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
            "name of a /usr/lib/subagents/<name>/ bundle to pull "
            "prompt/provider/model from; works for ephemeral and persistent "
            "subagents"
        ),
    )
    sp.set_defaults(func=cmd_spawn)

    rp = sub.add_parser(
        "reply",
        help="(child only) send a subagent:response to your parent (use --done for the final reply)",
    )
    rp.add_argument("--content", required=True, help="message text")
    rp.add_argument(
        "--done",
        action="store_true",
        help="terminating reply: emit the response and resolve own proc as completed",
    )
    rp.set_defaults(func=cmd_reply)

    dp = sub.add_parser(
        "done",
        help="(child only) finish after saving a durable result in the parent's workspace",
        description=(
            "Finish an ephemeral subagent. --result is interpreted inside "
            "$PAI_PARENT_HOME/workspace/$PAI_SLUG unless it already starts with "
            "workspace/ or is an absolute path. The file must exist; the parent "
            "receives a tiny subagent:response pointing at it."
        ),
    )
    dp.add_argument("--result", required=True, help="result file, normally result.md")
    dp.set_defaults(func=cmd_done)

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
        help="(parent only) abort a child subagent; subagents end themselves via `done --result`",
    )
    dn.add_argument("--slug", required=True, help="full slug as printed by spawn")
    dn.set_defaults(func=cmd_kill)

    args = parser.parse_args(argv)
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
