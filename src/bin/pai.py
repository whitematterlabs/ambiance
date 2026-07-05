"""pai — top-level user entrypoint.

Thin dispatcher; defers to `boot.init` (kernel) and the web console (UI)
without modifying either.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import os
import signal
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from boot.init import check_layout
from boot.paths import PAI_ROOT, REPO_ROOT


UPDATE_READY_NOTICE = "*** PAI is ready to update! ***"

# Where end-user (tarball) installs fetch releases. install.sh writes a
# var/lib/.release marker; its presence flips `pai update` from the git-pull
# path (dev checkout) to the download-and-swap path below.
DEFAULT_RELEASE_BASE = "https://github.com/whitematterlabs/pai/releases/latest/download"


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
        from usr.libexec.web.pai_web.server import run as web_run
        web_run(port=args.port, open_browser=not args.no_open)
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
    marker = _release_marker()
    if marker is not None:
        base = _release_base()
        try:
            latest = _latest_release_version(base)
        except SystemExit as e:
            print(f"pai start: update check skipped — {e}", file=sys.stderr)
            return
        latest_sha = _latest_release_sha(base)
        installed_sha = _installed_sha()
        print(f"installed: {marker}")
        print(f"latest: {latest}")
        if _tarball_up_to_date(marker, installed_sha, latest, latest_sha):
            print("status: up to date")
        else:
            print(UPDATE_READY_NOTICE)
            print("next: pai update")
        return
    try:
        status = _read_update_status(REPO_ROOT, fetch=True)
    except SystemExit as e:
        print(f"pai start: update check skipped — {e}", file=sys.stderr)
        return
    if status.upstream and status.behind and not status.ahead:
        print(UPDATE_READY_NOTICE)
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


def _kernel_is_running() -> bool:
    """True if a kernel currently holds its pid flock. Non-blocking probe that
    always releases — mirrors sbin/reboot so `update` can decide whether a
    restart is even applicable."""
    import fcntl

    lock = PAI_ROOT / "run" / "kernel.pid"
    if not lock.exists():
        return False
    try:
        fd = os.open(lock, os.O_RDWR)
    except OSError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True  # someone holds it → a kernel is up
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def _restart_kernel_after_update(new_ver: str, *, no_restart: bool) -> None:
    """Make the update fully live: tell the running kernel to re-exec into the
    freshly-repointed build. Without this the kernel keeps running the old build
    until a manual `sbin/reboot`, which is exactly how new-web/old-kernel skew
    (silent split-brain) happened. `--no-restart` opts out to stage a build."""
    if no_restart:
        print("pai update: staged; kernel NOT restarted (--no-restart). "
              "run `sbin/reboot` to go live.")
        return
    if not _kernel_is_running():
        return
    try:
        from boot.processes import emit_event

        emit_event({"kind": "kernel:restart", "source": "update"})
        print(f"pai update: restarting kernel into {new_ver}")
    except Exception as e:  # never fail the update over the restart nudge
        print(f"pai update: could not signal kernel restart ({e!r}); "
              "run `sbin/reboot` to go live.", file=sys.stderr)


# ---------- tarball (end-user) update path ----------

def _release_marker() -> str | None:
    """Return the installed version recorded by install.sh, or None for a dev
    (git) checkout. Its presence routes `pai update` through the tarball path."""
    try:
        text = (PAI_ROOT / "var" / "lib" / ".release").read_text().strip()
    except OSError:
        return None
    return text or None


def _release_base() -> str:
    return os.environ.get("PAI_RELEASE_BASE", DEFAULT_RELEASE_BASE)


def _installed_sha() -> str | None:
    """The sha256 of the tarball this install was provisioned from, or None if
    unrecorded (older install, or a dev checkout). Recorded alongside the
    version marker so `pai update` can detect a *same-version rebuild* — the
    release ships a rolling `latest` tarball under a stable version string, so
    version equality alone is not enough to know we're current."""
    try:
        text = (PAI_ROOT / "var" / "lib" / ".release.sha256").read_text().strip()
    except OSError:
        return None
    return text.split()[0] if text else None


def _write_sha_marker(sha: str) -> None:
    dest = PAI_ROOT / "var" / "lib" / ".release.sha256"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(f"{sha}  pai.tar.gz\n")


def _clear_sha_marker() -> None:
    (PAI_ROOT / "var" / "lib" / ".release.sha256").unlink(missing_ok=True)


def _latest_release_sha(base: str) -> str:
    """The published tarball's sha256, or '' if the release has no sha asset
    (e.g. a hand-cut release). Returning '' makes callers fall back to
    version-only comparison rather than forcing a needless re-download."""
    try:
        text = _download_text(f"{base}/pai.tar.gz.sha256").strip()
    except SystemExit:
        return ""
    return text.split()[0] if text else ""


def _tarball_up_to_date(
    current_ver: str, installed_sha: str | None, latest: str, latest_sha: str
) -> bool:
    """True when the installed tarball matches the published one. A newer
    version is always an update; same version is current only if the published
    sha is absent (version-only) or matches what we installed."""
    if latest != current_ver:
        return False
    if not latest_sha:
        return True
    return latest_sha == installed_sha


def _opt_pai() -> Path:
    return PAI_ROOT / "opt" / "pai"


def _download_text(url: str) -> str:
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            return resp.read().decode("utf-8")
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise SystemExit(f"pai update: could not fetch {url}: {e}") from e


def _download(url: str, dest: Path) -> None:
    try:
        with urllib.request.urlopen(url, timeout=300) as resp:
            data = resp.read()
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise SystemExit(f"pai update: download failed {url}: {e}") from e
    dest.write_bytes(data)


def _latest_release_version(base: str) -> str:
    ver = _download_text(f"{base}/version.txt").strip()
    if not ver:
        raise SystemExit(f"pai update: empty version.txt at {base}")
    return ver


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _installed_versions() -> list[str]:
    """Version dir names under opt/pai, newest first; excludes the `current`
    symlink."""
    opt = _opt_pai()
    if not opt.is_dir():
        return []
    dirs = [d for d in opt.iterdir() if d.is_dir() and not d.is_symlink()]
    dirs.sort(key=lambda d: d.stat().st_mtime, reverse=True)
    return [d.name for d in dirs]


def _repoint_current(ver: str) -> None:
    link = _opt_pai() / "current"
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(ver)  # relative to opt/pai/


def _write_release_marker(ver: str) -> None:
    dest = PAI_ROOT / "var" / "lib" / ".release"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(f"{ver}\n")


def _gc_versions(keep: int = 2) -> None:
    """Retain the `keep` newest version dirs (plus whatever `current` points
    at); remove older ones."""
    opt = _opt_pai()
    link = opt / "current"
    current = link.resolve() if link.is_symlink() else None
    dirs = [d for d in opt.iterdir() if d.is_dir() and not d.is_symlink()]
    dirs.sort(key=lambda d: d.stat().st_mtime, reverse=True)
    for d in dirs[keep:]:
        if current is not None and d.resolve() == current:
            continue
        shutil.rmtree(d, ignore_errors=True)


def _reprovision_tarball(ver_dir: Path) -> int:
    """Sync the env from the lockfile and re-run paifs-init from `ver_dir` so
    every FHS symlink/.pth/shim repoints at this version. Web dist/ ships
    prebuilt in the tarball, so there is no pnpm step."""
    uv = shutil.which("uv")
    if uv is None:
        print(
            "pai update: `uv` is required to reprovision; install uv and rerun.",
            file=sys.stderr,
        )
        return 1
    print("==> uv sync")
    _run_checked([uv, "sync"], cwd=ver_dir)
    print("==> paifs-init")
    _run_checked([uv, "run", "paifs-init", "--no-setup"], cwd=ver_dir)
    return 0


def _download_and_extract(base: str, ver: str, expected_sha: str = "") -> tuple[Path, str]:
    """Download + verify the release tarball and extract it to opt/pai/<ver>.

    Returns (ver_dir, actual_sha) so the caller can record exactly what it
    installed. `expected_sha`, when given, is verified against (the caller has
    usually already fetched it); otherwise the sha asset is fetched here."""
    ver_dir = _opt_pai() / ver
    with tempfile.TemporaryDirectory(prefix="pai-update-") as tmp:
        tarball = Path(tmp) / "pai.tar.gz"
        print("==> downloading pai.tar.gz")
        _download(f"{base}/pai.tar.gz", tarball)
        if not expected_sha:
            try:
                expected_sha = _download_text(f"{base}/pai.tar.gz.sha256").split()[0]
            except SystemExit:
                expected_sha = ""
        actual = _sha256(tarball)
        if expected_sha and actual != expected_sha:
            raise SystemExit(
                f"pai update: checksum mismatch (expected {expected_sha}, got {actual})"
            )
        if ver_dir.exists():
            shutil.rmtree(ver_dir)
        ver_dir.mkdir(parents=True)
        print(f"==> extracting to {ver_dir}")
        with tarfile.open(tarball) as tf:
            tf.extractall(ver_dir, filter="tar")
    return ver_dir, actual


def _cmd_update_tarball(args: argparse.Namespace, current_ver: str) -> int:
    if args.rollback:
        return _rollback_tarball(current_ver, no_restart=args.no_restart)

    base = _release_base()
    try:
        latest = _latest_release_version(base)
    except SystemExit as e:
        if args.check:
            print(f"installed: {current_ver}")
            print(f"status: could not reach release server — {e}", file=sys.stderr)
            return 0
        raise

    latest_sha = _latest_release_sha(base)
    installed_sha = _installed_sha()
    up_to_date = _tarball_up_to_date(current_ver, installed_sha, latest, latest_sha)

    print(f"installed: {current_ver}")
    print(f"latest: {latest}")

    if args.check:
        if up_to_date:
            print("status: up to date")
        elif latest != current_ver:
            print(f"status: update available ({latest})")
            print("next: pai update")
        else:
            print(f"status: update available ({current_ver} — new build)")
            print("next: pai update")
        return 0

    if up_to_date:
        print("pai update: already on the latest release")
        return 0

    ver_dir, sha = _download_and_extract(base, latest, latest_sha)
    if args.no_reprovision:
        print("pai update: extracted; skipped reprovision")
    else:
        rc = _reprovision_tarball(ver_dir)
        if rc != 0:
            return rc
    _repoint_current(latest)
    _write_release_marker(latest)
    _write_sha_marker(sha)
    _gc_versions(keep=2)
    print(f"pai update: now on {latest}")
    _restart_kernel_after_update(latest, no_restart=args.no_restart)
    return 0


def _rollback_tarball(current_ver: str, *, no_restart: bool = False) -> int:
    candidates = [v for v in _installed_versions() if v != current_ver]
    if not candidates:
        print("pai update: no prior version to roll back to", file=sys.stderr)
        return 1
    prior = candidates[0]
    print(f"==> rolling back to {prior}")
    rc = _reprovision_tarball(_opt_pai() / prior)
    if rc != 0:
        return rc
    _repoint_current(prior)
    _write_release_marker(prior)
    # The prior dir wasn't re-downloaded, so we don't know its tarball sha;
    # drop the marker so the next `pai update` treats the build as unknown and
    # re-syncs rather than trusting a sha from the rolled-forward version.
    _clear_sha_marker()
    print(f"pai update: rolled back to {prior}")
    _restart_kernel_after_update(prior, no_restart=no_restart)
    return 0


def cmd_update(args: argparse.Namespace) -> int:
    marker = _release_marker()
    if marker is not None:
        return _cmd_update_tarball(args, marker)
    if getattr(args, "rollback", False):
        print(
            "pai update: --rollback applies to tarball installs only "
            "(no var/lib/.release marker found)",
            file=sys.stderr,
        )
        return 1

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
    rc = _reprovision_after_update(REPO_ROOT, no_web=args.no_web)
    if rc == 0:
        # A dev checkout runs its source in place; a git pull already swapped the
        # files, so a re-exec is what actually adopts them.
        _restart_kernel_after_update("dev", no_restart=args.no_restart)
    return rc


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="pai", description="PAI user entrypoint")
    sub = ap.add_subparsers(dest="cmd", required=True)

    start = sub.add_parser(
        "start", help="start the kernel and the web console together"
    )
    start.add_argument(
        "--headless",
        action="store_true",
        help="run only the kernel (no UI); equivalent to `init`",
    )
    start.add_argument(
        "--port",
        type=int,
        default=8787,
        help="web console port (default 8787)",
    )
    start.add_argument(
        "--no-open",
        action="store_true",
        help="don't auto-open a browser",
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
    update.add_argument(
        "--rollback",
        action="store_true",
        help="(tarball installs) repoint to the previous installed version",
    )
    update.add_argument(
        "--no-restart",
        action="store_true",
        help="stage the new build without rebooting the running kernel "
        "(the kernel keeps running the old build until `sbin/reboot`)",
    )
    update.set_defaults(func=cmd_update)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
