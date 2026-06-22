"""pai — top-level user entrypoint.

Thin dispatcher; defers to `boot.init` (kernel) and `sbin.tui` (UI) without
modifying either.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import os
import signal
import shutil
import subprocess
import sys
from pathlib import Path

from boot.init import check_layout
from boot.paths import PAI_ROOT, REPO_ROOT


@dataclass(frozen=True)
class UpdateStatus:
    repo: Path
    branch: str
    upstream: str | None
    ahead: int
    behind: int
    dirty: bool
    remote_url: str | None


def cmd_start(args: argparse.Namespace) -> int:
    _check_for_update_on_start()

    missing = check_layout(PAI_ROOT)
    if missing:
        print(
            f"pai: PAI_ROOT={PAI_ROOT} missing required dirs: {', '.join(missing)}\n"
            f"     run `paifs-init` to lay out the skeleton.",
            file=sys.stderr,
        )
        return 1

    if args.headless:
        os.execvp(sys.executable, [sys.executable, "-u", "-m", "boot.entry"])
        raise AssertionError("execvp returned without replacing process")

    log_path = PAI_ROOT / "var" / "log" / "kernel" / "kernel.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_fp = log_path.open("a", buffering=1, encoding="utf-8")
    kernel = subprocess.Popen(
        [sys.executable, "-u", "-m", "boot.entry"],
        start_new_session=True,
        stdout=log_fp,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
    )
    try:
        if args.web:
            from usr.libexec.web.pai_web.server import run as web_run
            web_run(port=args.port, open_browser=not args.no_open)
        else:
            from sbin.tui import main as tui_main
            tui_main()
    finally:
        if kernel.poll() is None:
            # Signal the kernel's whole process group, not just the leader —
            # if the kernel itself is wedged, this still tears down its
            # driver subprocesses (chromium, tmux, etc).
            try:
                pgid = os.getpgid(kernel.pid)
            except ProcessLookupError:
                pgid = None
            if pgid is not None:
                try:
                    os.killpg(pgid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            try:
                kernel.wait(timeout=5)
            except subprocess.TimeoutExpired:
                if pgid is not None:
                    try:
                        os.killpg(pgid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                kernel.wait()
    return kernel.returncode or 0


def _check_for_update_on_start() -> None:
    print("==> update check")
    try:
        status = _read_update_status(REPO_ROOT, fetch=True)
    except SystemExit as e:
        print(f"pai start: update check skipped — {e}", file=sys.stderr)
        return
    _print_update_status(status)


def _git_output(repo: Path, *args: str, required: bool = True) -> str | None:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(repo),
            check=required,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as e:
        raise SystemExit("pai update: `git` not found on PATH") from e
    except subprocess.CalledProcessError as e:
        if required:
            raise SystemExit(
                f"pai update: git command failed: git {' '.join(args)}"
            ) from e
        return None
    return proc.stdout.strip()


def _run_checked(cmd: list[str], *, cwd: Path) -> None:
    try:
        subprocess.run(cmd, cwd=str(cwd), check=True)
    except FileNotFoundError as e:
        raise SystemExit(f"pai update: `{cmd[0]}` not found on PATH") from e
    except subprocess.CalledProcessError as e:
        raise SystemExit(f"pai update: command failed: {' '.join(cmd)}") from e


def _read_update_status(repo: Path, *, fetch: bool) -> UpdateStatus:
    inside = _git_output(repo, "rev-parse", "--is-inside-work-tree", required=False)
    if inside != "true":
        raise SystemExit(f"pai update: {repo} is not a git checkout")

    branch = _git_output(repo, "branch", "--show-current") or "HEAD"
    upstream = _git_output(
        repo,
        "rev-parse",
        "--abbrev-ref",
        "--symbolic-full-name",
        "@{u}",
        required=False,
    )
    remote_url = _git_output(repo, "remote", "get-url", "origin", required=False)

    if fetch and upstream:
        _run_checked(["git", "fetch", "--quiet", "--prune"], cwd=repo)

    ahead = 0
    behind = 0
    if upstream:
        counts = _git_output(
            repo,
            "rev-list",
            "--left-right",
            "--count",
            f"HEAD...{upstream}",
        )
        if counts:
            ahead_s, behind_s = counts.split()
            ahead = int(ahead_s)
            behind = int(behind_s)

    dirty = bool(_git_output(repo, "status", "--porcelain"))
    return UpdateStatus(
        repo=repo,
        branch=branch,
        upstream=upstream,
        ahead=ahead,
        behind=behind,
        dirty=dirty,
        remote_url=remote_url,
    )


def _print_update_status(status: UpdateStatus) -> None:
    print(f"source: {status.repo}")
    if status.remote_url:
        print(f"remote: {status.remote_url}")
    if status.upstream:
        print(f"branch: {status.branch} -> {status.upstream}")
    else:
        print(f"branch: {status.branch} (no upstream)")
    print(f"local changes: {'yes' if status.dirty else 'no'}")

    if not status.upstream:
        print("status: cannot check updates without an upstream branch")
    elif status.ahead and status.behind:
        print(f"status: diverged ({status.ahead} ahead, {status.behind} behind)")
    elif status.behind:
        print(f"status: update available ({status.behind} commit(s) behind)")
        print("next: pai update")
    elif status.ahead:
        print(f"status: local branch is {status.ahead} commit(s) ahead")
    else:
        print("status: up to date")


def _reprovision_after_update(repo: Path, *, no_web: bool) -> int:
    uv = shutil.which("uv")
    if uv is None:
        print(
            "pai update: `uv` is required to reprovision; install uv and rerun.",
            file=sys.stderr,
        )
        return 1

    print("==> uv sync")
    _run_checked([uv, "sync"], cwd=repo)

    web_dir = repo / "src" / "usr" / "libexec" / "web"
    pnpm = shutil.which("pnpm")
    if not no_web and web_dir.is_dir():
        if pnpm is None:
            print("==> web frontend skipped: `pnpm` not found", file=sys.stderr)
        else:
            print("==> web frontend (pnpm)")
            _run_checked([pnpm, "install"], cwd=web_dir)
            _run_checked([pnpm, "build"], cwd=web_dir)

    print("==> paifs-init")
    _run_checked([uv, "run", "paifs-init", "--no-setup"], cwd=repo)
    return 0


def cmd_update(args: argparse.Namespace) -> int:
    status = _read_update_status(REPO_ROOT, fetch=not args.no_fetch)
    _print_update_status(status)

    if args.check:
        return 0
    if not status.upstream:
        print("pai update: refusing to update without an upstream branch", file=sys.stderr)
        return 1
    if status.behind == 0:
        print("pai update: no source update needed")
        return 0
    if status.dirty:
        print(
            "pai update: refusing to update with local changes; commit or stash them first",
            file=sys.stderr,
        )
        return 1
    if status.ahead and status.behind:
        print("pai update: refusing to update a diverged branch", file=sys.stderr)
        return 1

    _run_checked(["git", "pull", "--ff-only"], cwd=REPO_ROOT)
    if args.no_reprovision:
        print("pai update: skipped reprovision")
        return 0
    return _reprovision_after_update(REPO_ROOT, no_web=args.no_web)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="pai", description="PAI user entrypoint")
    sub = ap.add_subparsers(dest="cmd", required=True)

    start = sub.add_parser("start", help="start kernel and an owner surface together")
    start.add_argument(
        "--headless",
        action="store_true",
        help="run only the kernel (no UI); equivalent to `init`",
    )
    start.add_argument(
        "--web",
        action="store_true",
        help="run the web surface instead of the terminal TUI",
    )
    start.add_argument(
        "--port",
        type=int,
        default=8787,
        help="web surface port (with --web; default 8787)",
    )
    start.add_argument(
        "--no-open",
        action="store_true",
        help="don't auto-open a browser (with --web)",
    )
    start.set_defaults(func=cmd_start)

    update = sub.add_parser(
        "update",
        help="update the PAI source checkout and runtime shims",
    )
    update.add_argument(
        "--check",
        action="store_true",
        help="only report whether an update is available",
    )
    update.add_argument(
        "--no-fetch",
        action="store_true",
        help="use local git refs without fetching from the upstream first",
    )
    update.add_argument(
        "--no-web",
        action="store_true",
        help="skip rebuilding the web frontend after updating",
    )
    update.add_argument(
        "--no-reprovision",
        action="store_true",
        help="pull source only; skip uv sync and paifs-init",
    )
    update.set_defaults(func=cmd_update)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
