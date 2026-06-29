"""Boot entrypoint, executed by /sbin/init via execvp.

Runs phases 1–6 synchronously, then enters phase 7 (the asyncio
supervise loop) by delegating to boot.main.run().
"""
from __future__ import annotations

import asyncio
import atexit
import errno
import fcntl
import os
import re
import signal
import subprocess
import sys
import traceback

from . import paths
from .phases import backfill, clean, hooks, probe, reconcile, sanity, start
from . import main as supervise

_LOCK_FILE = paths.PAI_ROOT / "run" / "kernel.pid"

# Matches the kernel's own module invocation in a process command line:
# `-m boot.entry`, `-m boot`, or `-m boot run` (the __main__ arg form). Does
# NOT match sibling modules like `-m boot.tui` or `-m boot.something`.
_KERNEL_CMD_RE = re.compile(r"-m\s+boot(\.entry)?(\s|$)")

# Held for the lifetime of the kernel process. flock() releases automatically
# on close (including SIGKILL), so we cannot leave a stale lock behind.
_lock_fd: int | None = None


def _acquire_pid_lock() -> bool:
    """Take an exclusive flock on run/kernel.pid; return False if another
    kernel already holds it. Writes our PID to the file as a human-readable
    breadcrumb (the *lock*, not the file contents, is what enforces mutex)."""
    global _lock_fd
    _LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(_LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        # The PID-file *contents* can drift from the actual lock holder
        # (e.g. a forked-then-exited parent inherits its PID into the file
        # while the child holds the lock). lsof is the source of truth for
        # who holds the fd; fall back to file contents only if that fails.
        holder = "?"
        try:
            lsof = paths.host_executable("lsof")
            if lsof is None:
                raise FileNotFoundError("lsof")
            out = subprocess.run(
                [lsof, "-t", str(_LOCK_FILE)],
                capture_output=True,
                text=True,
                timeout=2,
            )
            pids = [p for p in out.stdout.split() if p.strip()]
            if pids:
                holder = ",".join(pids)
        except Exception:
            pass
        if holder == "?":
            try:
                holder = os.read(fd, 64).decode().strip() or "?"
            except OSError:
                pass
        os.close(fd)
        print(
            f"[boot] kernel already running (pid={holder}); exiting",
            file=sys.stderr,
            flush=True,
        )
        return False
    os.ftruncate(fd, 0)
    os.write(fd, f"{os.getpid()}\n".encode())
    _lock_fd = fd
    atexit.register(_release_pid_lock)
    return True


def _find_duplicate_kernel_pids(ps_output: str, self_pid: int) -> list[int]:
    """Parse `ps -axww -o pid=,command=` output and return the PIDs of *other*
    live kernels for this PAI_ROOT.

    A kernel is our runtime interpreter (the FHS venv python, whose path embeds
    PAI_ROOT — or whatever `sys.executable` we re-exec with) running the
    `boot`/`boot.entry` module. Matching on our own interpreter path scopes the
    search to this root, so a kernel for a *different* PAI_ROOT is never
    targeted. Self is always excluded.
    """
    venv = str(paths.venv_python())
    exe = sys.executable
    pids: list[int] = []
    for line in ps_output.splitlines():
        line = line.strip()
        if not line:
            continue
        head, _, cmd = line.partition(" ")
        try:
            pid = int(head)
        except ValueError:
            continue
        if pid <= 0 or pid == self_pid:
            continue
        if not _KERNEL_CMD_RE.search(cmd):
            continue
        if venv not in cmd and exe not in cmd:
            continue
        pids.append(pid)
    return pids


def _reap_duplicate_kernels() -> None:
    """Evict stragglers once we hold the flock.

    The flock makes us the sole *legitimate* kernel for this PAI_ROOT, but it
    only blocks lock-aware kernels that start *after* us. A kernel left running
    from a prior boot that predated the flock (or was launched by a path that
    bypassed it) keeps running our drivers, racing us on shared driver state —
    e.g. two kernels writing the same `cursor.yaml.tmp`, where one's
    `os.replace` renames the tmp out from under the other and crash-loops the
    driver. Single-writer is the invariant; enforce it actively.

    Conservative by design: matches only our own interpreter on the boot
    module, never self, and SIGTERMs (graceful — the straggler runs its own
    orderly shutdown) rather than SIGKILL. The intervening boot phases give it
    ample time to drain before our drivers start.
    """
    ps = paths.host_executable("ps")
    if ps is None:
        return
    try:
        out = subprocess.run(
            [ps, "-axww", "-o", "pid=,command="],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return
    for pid in _find_duplicate_kernel_pids(out, os.getpid()):
        print(
            f"[boot] reaping duplicate kernel pid={pid} — we hold the flock; "
            "it predates or bypassed it (SIGTERM)",
            file=sys.stderr,
            flush=True,
        )
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError) as e:
            print(
                f"[boot] could not signal duplicate kernel pid={pid}: {e!r}",
                file=sys.stderr,
                flush=True,
            )


def _release_pid_lock() -> None:
    global _lock_fd
    if _lock_fd is None:
        return
    try:
        fcntl.flock(_lock_fd, fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        os.close(_lock_fd)
    except OSError:
        pass
    _lock_fd = None


def _ensure_stable_cwd() -> None:
    """Pin the kernel's cwd to $PAI_ROOT, which never moves at runtime.

    The kernel is typically launched from the release dir (e.g.
    `~/.pai/opt/pai/<ver>/`). That directory is *swappable*: `pai update`
    deletes and re-extracts it under the running process. Once the cwd inode
    is gone, `os.getcwd()` and any cwd-less subprocess spawn raise a bare
    `FileNotFoundError(2, 'No such file or directory')` — which surfaces as a
    fatal "nudge failed" mid-turn and reaps whatever subagent was running.
    $PAI_ROOT itself is stable (only `opt/` underneath it gets swapped), so
    chdir'ing here makes getcwd and inherited-cwd children resolve against a
    directory that survives updates and re-execs.
    """
    try:
        os.chdir(paths.PAI_ROOT)
    except OSError as e:
        # Pre-sanity: a missing root is reported cleanly by sanity.run() below.
        print(f"[boot] warn: could not chdir to PAI_ROOT: {e!r}", flush=True)


def boot() -> int:
    # First, before getcwd or any child spawn can trip over a swapped-out
    # release dir: pin cwd to the stable $PAI_ROOT.
    _ensure_stable_cwd()
    # Before anything spawns a child: ensure the PAI bin dirs are on PATH. A
    # Finder-launched .app gives us no shell PATH, so without this the kernel's
    # subprocesses (services, hooks, header helpers) can't find PAI tools.
    paths.prepend_pai_path()
    if not _acquire_pid_lock():
        return 1
    # We hold the flock, so we are the one legitimate kernel. Evict any
    # straggler kernel from a prior boot that predated/bypassed the lock
    # before the boot phases (and our drivers) start writing shared state.
    _reap_duplicate_kernels()
    try:
        sanity.run()
        clean.run()
        probe.run()
        reconcile.run()
        start.run()
        hooks.run()
        backfill.run()
    except sanity.SanityError as e:
        print(f"[boot] sanity failed: {e}", file=sys.stderr, flush=True)
        return 1
    except Exception as e:
        tb = traceback.format_exc()
        print(f"[boot] phase failed: {e!r}\n{tb}", file=sys.stderr, flush=True)
        return 2
    try:
        asyncio.run(supervise.run())
    except KeyboardInterrupt:
        pass
    except BaseException:
        tb = traceback.format_exc()
        print(f"[kernel] fatal: uncaught in supervise.run()\n{tb}", file=sys.stderr, flush=True)
        try:
            from datetime import datetime
            crash_dir = paths.PAI_ROOT / "var" / "log" / "kernel"
            crash_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%dT%H%M%S")
            (crash_dir / f"crash-{ts}.log").write_text(tb)
        except Exception:
            pass
        return 3
    if supervise._restart_requested:
        print("[boot] re-exec for kernel:restart", flush=True)
        _release_pid_lock()  # drop lock so the re-exec can re-acquire
        os.execvp(sys.executable, [sys.executable, "-u", "-m", "boot.entry"])
        raise AssertionError("execvp returned without replacing process")
    return 0


if __name__ == "__main__":
    sys.exit(boot())
