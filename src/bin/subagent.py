#!/usr/bin/env python
"""subagent — spawn and manage subordinate PAI instances.

Usage:
    subagent list                                         # installed packages you can spawn with --package
    subagent spawn --slug NAME --prompt '...'             # fork a subagent, return its pid
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
`$PAI_RESULT_DIR/`, then calls
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

from boot import config as C
from boot import image_refs
from boot import processes as P
from boot import stitch as S


DATE_SUFFIX = re.compile(r"-\d{4}-\d{2}-\d{2}(?:T\d{2}-\d{2}-\d{2})?$")
SHELL_MANGLED_COMMA_CURRENCY = re.compile(
    r"(?is)\b(?:budget|rent|price|cost|usd|dollars?|amount)\b[^\n]{0,120}(?<!\d),\d{3}\b"
)
SHELL_MANGLED_DECIMAL_K = re.compile(
    r"(?is)\b(?:budget|rent|price|cost|usd|dollars?|amount)\b[^\n]{0,120}(?<![\d])\.\d+\s*k\b"
)

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


def _prompt_looks_shell_mangled(prompt: str) -> bool:
    return bool(
        SHELL_MANGLED_COMMA_CURRENCY.search(prompt)
        or SHELL_MANGLED_DECIMAL_K.search(prompt)
    )


def _resolve_spawn_prompt(args: argparse.Namespace) -> str | None:
    prompt = args.prompt

    if prompt is not None and not prompt.strip():
        print("error: subagent prompt is empty", file=sys.stderr)
        return None

    if prompt is not None and _prompt_looks_shell_mangled(prompt):
        print(
            "error: subagent prompt looks shell-mangled: a budget/price contains "
            "`,200` or `.5k`, which often means a value like `$1,200` or `$1.5k` "
            "was passed inside double quotes and the shell expanded `$N` as a "
            "positional parameter. Retry "
            "with single quotes around the --prompt value.",
            file=sys.stderr,
        )
        return None

    return prompt


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
    prompt = _resolve_spawn_prompt(args)
    if prompt is None and args.prompt is not None:
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
    if not args.persistent and not prompt:
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
        prompt
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
        P.emit_event({
            "source": "subagent",
            "kind": "pai_message",
            "target_pid": child_pid,
            "sender_pid": parent_pid,
            "text": prompt,
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
            f"/proc the instant it finishes, so a poll loop just races the reap. "
            f"To steer it, add context, or answer a question it raised: "
            f"`bin/send-message --to {child_pid} --content '...'` (then end "
            f"your turn again)."
        )
    return 0


def _read_child_reply_env(
    command: str,
    *,
    require_slug: bool = False,
) -> tuple[int, int, str] | None:
    sender_raw = os.environ.get("PAI_PID")
    parent_raw = os.environ.get("PAI_PARENT")
    env_slug = os.environ.get("PAI_SLUG")
    if not sender_raw:
        print(f"error: $PAI_PID not set — {command} must be invoked from a PAI turn", file=sys.stderr)
        return None
    if not parent_raw:
        print(f"error: $PAI_PARENT not set — only subagents can use `{command}`", file=sys.stderr)
        return None
    try:
        sender_pid = int(sender_raw)
        parent_pid = int(parent_raw)
    except ValueError:
        print("error: $PAI_PID/$PAI_PARENT must be ints", file=sys.stderr)
        return None
    child_slug = _canonical_slug_for_pid(
        sender_pid,
        env_slug=env_slug,
        command=command,
        require=require_slug,
    )
    if child_slug is None:
        return None
    return sender_pid, parent_pid, child_slug


def _canonical_slug_for_pid(
    pid: int,
    *,
    env_slug: str | None,
    command: str,
    require: bool = True,
) -> str | None:
    try:
        live_slug = P.find_pai_slug(pid)
    except P.ProcessNotFound:
        if require:
            print(f"error: own proc for $PAI_PID={pid} not found — required for `{command}`", file=sys.stderr)
            return None
        return env_slug or ""
    if env_slug and env_slug != live_slug:
        print(
            f"warning: $PAI_SLUG={env_slug!r} does not match live proc "
            f"slug {live_slug!r} for pid={pid}; using live slug",
            file=sys.stderr,
        )
    return live_slug


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

    result_dir_raw = os.environ.get("PAI_RESULT_DIR")
    result_dir = Path(result_dir_raw) if result_dir_raw else parent_home / "workspace" / child_slug
    legacy_result_dir = parent_home / "workspace" / child_slug

    if raw.is_absolute():
        candidates = [raw]
    elif raw.parts and raw.parts[0] == "workspace":
        candidates = [parent_home / raw]
    else:
        candidates = [result_dir / raw]
        if result_dir.resolve(strict=False) != legacy_result_dir.resolve(strict=False):
            candidates.append(legacy_result_dir / raw)

    allowed_dirs = []
    for base in (result_dir, legacy_result_dir):
        if base not in allowed_dirs:
            allowed_dirs.append(base)

    missing: list[Path] = []
    for result_path in candidates:
        result_resolved = result_path.resolve()
        matched_relative: Path | None = None
        for base in allowed_dirs:
            try:
                matched_relative = result_resolved.relative_to(base.resolve(strict=False))
                break
            except ValueError:
                continue
        if matched_relative is None:
            continue
        if not result_resolved.is_file():
            missing.append(result_resolved)
            continue
        return (Path("workspace") / child_slug / matched_relative).as_posix()

    candidate_display = ", ".join(str(p.resolve(strict=False)) for p in candidates)
    if missing:
        print(
            f"error: result file not found: {candidate_display}. "
            "Write the report before calling `subagent done`.",
            file=sys.stderr,
        )
        return None

    print(
        "error: --result must point inside $PAI_RESULT_DIR "
        f"(or $PAI_PARENT_HOME/workspace/{child_slug}; got {result!r})",
        file=sys.stderr,
    )
    return None


def _absolutize_result_refs(result_abs: Path) -> None:
    """Rewrite relative attachment paths inside a finished result.md to absolute.

    Best-effort: a child saves screenshots/files relative to its cwd (which is
    where result.md lives), so `![shot](workspace/x/shot.png)` only resolves
    against that dir. Absolutize against result.md's directory so the parent —
    and ultimately the owner's browser — can reach the file. A missing/unreadable
    result or an unchanged body is a no-op."""
    try:
        text = result_abs.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return
    rewritten = image_refs.absolutize_local_refs(text, result_abs.parent)
    if rewritten != text:
        try:
            result_abs.write_text(rewritten, encoding="utf-8")
        except OSError:
            pass


def cmd_reply(args: argparse.Namespace) -> int:
    env = _read_child_reply_env("reply", require_slug=True)
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

    # The child references files it saved relative to its own cwd (where
    # result.md lives). Once the parent copies those refs into its owner-facing
    # reply that cwd is gone and the path 404s in the console. Absolutize them
    # against result.md's directory now, so the hand-off carries a real path.
    parent_home = _parent_home(parent_pid)
    if parent_home is not None:
        _absolutize_result_refs((parent_home / result).resolve())

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
    child_slug = _canonical_slug_for_pid(
        sender_pid,
        env_slug=os.environ.get("PAI_SLUG"),
        command="plan-ready",
    )
    if child_slug is None:
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
        "slug": child_slug,
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


def cmd_list(args: argparse.Namespace) -> int:
    """Enumerate installed subagent packages so a PAI can discover what it can
    spawn with `--package` — the only way to see them short of `ls
    /usr/lib/subagents/`."""
    root = C.SUBAGENTS_DIR
    rows: list[tuple[str, str]] = []
    if root.is_dir():
        for entry in sorted(root.iterdir()):
            if entry.name.startswith(".") or not (entry / "package.yaml").is_file():
                continue
            try:
                desc = (C.resolve_subagent_package(entry.name).get("description") or "").strip()
            except Exception:
                desc = ""
            rows.append((entry.name, desc))
    if not rows:
        print("no subagent packages installed (looked in /usr/lib/subagents/)")
        return 0
    width = max(len(name) for name, _ in rows)
    for name, desc in rows:
        print(f"{name.ljust(width)}  {desc}".rstrip())
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

    ls = sub.add_parser(
        "list",
        help="list installed subagent packages you can spawn with --package",
        description=(
            "Print the installed subagent bundles at /usr/lib/subagents/<name>/ "
            "with their descriptions. Use a name here as `spawn --package <name>`."
        ),
    )
    ls.set_defaults(func=cmd_list)

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
        help=(
            "task for the subagent (required for ephemeral; optional for "
            "--persistent). Use single quotes if it contains dollar amounts."
        ),
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
            "$PAI_RESULT_DIR first, then the legacy parent workspace path, unless "
            "it already starts with workspace/ or is an absolute path. The file must exist; the parent "
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
