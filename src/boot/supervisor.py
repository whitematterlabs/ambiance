"""Supervisor — forks and tracks background subprocesses declared by specs.

A proc whose spec has `run:` is a background service. This module forks
the command, tees its stdout/stderr into `log.md`, awaits its exit, and
applies the `restart:` policy (or resolves the proc). Boot-resume
re-forks running procs according to policy when the kernel starts.

Cron services (spec has `schedule:`) are NOT supervised here persistently;
their per-fire subprocesses are launched by `fire_once()` and not registered
in `_handles` — they're transient.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from . import processes as P


def _spawn_args(run):
    """Resolve a spec's `run:` to argv + shell flag.

    String → routed through `sh -c` (shell semantics: pipes, loops, redirects).
    List   → exec'd directly (no shell parent, clean argv, honest signals).
    """
    if isinstance(run, str):
        return ["/bin/sh", "-c", run]
    return list(run)


@dataclass
class _Handle:
    slug: str
    spec: dict
    proc: asyncio.subprocess.Process
    waiter: asyncio.Task


_handles: dict[str, _Handle] = {}


async def _tee_stream(stream: asyncio.StreamReader, slug: str, tag: str) -> None:
    """Read lines from stream and append to the proc's log.md with a tag.

    Returns on EOF. Silently stops if the proc dir disappears.
    """
    while True:
        line = await stream.readline()
        if not line:
            return
        text = line.decode("utf-8", errors="replace").rstrip("\n")
        try:
            P.append_log(slug, f"{tag}: {text}")
        except P.ProcessNotFound:
            return


async def _await_exit(slug: str) -> None:
    """Wait for the tracked subprocess to exit; then resolve or restart."""
    handle = _handles.get(slug)
    if handle is None:
        return
    rc = await handle.proc.wait()
    _handles.pop(slug, None)

    try:
        status = P.read_status(slug)
    except P.ProcessNotFound:
        return

    if status != "running":
        # Already resolved externally (cancelled, expired). Leave it alone.
        try:
            P.append_log(slug, f"kernel: subprocess exited rc={rc} (status={status})")
        except P.ProcessNotFound:
            pass
        return

    restart = handle.spec.get("restart", "never")
    if restart == "always" or (restart == "on-failure" and rc != 0):
        P.append_log(slug, f"kernel: subprocess exited rc={rc}, restarting ({restart})")
        await start(slug, handle.spec)
        return

    final = "completed" if rc == 0 else "failed"
    P.append_log(slug, f"kernel: subprocess exited rc={rc}")
    P.resolve(slug, final)


async def start(slug: str, spec: dict) -> None:
    """Fork a subprocess for a background service. spec must contain `run:`."""
    run = spec.get("run")
    if not run:
        raise ValueError(f"spec for {slug!r} has no 'run' field")
    if slug in _handles:
        return  # already tracked — idempotent on repeated spec events

    cmd = _spawn_args(run)

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(P.HOME_DIR),
    )
    P.append_log(slug, f"kernel: subprocess started pid={proc.pid} ({run})")

    asyncio.create_task(_tee_stream(proc.stdout, slug, "stdout"), name=f"tee-out-{slug}")
    asyncio.create_task(_tee_stream(proc.stderr, slug, "stderr"), name=f"tee-err-{slug}")
    waiter = asyncio.create_task(_await_exit(slug), name=f"wait-{slug}")
    _handles[slug] = _Handle(slug=slug, spec=spec, proc=proc, waiter=waiter)


async def stop(slug: str, grace: float = 5.0) -> None:
    """Stop a tracked subprocess. SIGTERM, then SIGKILL after `grace` seconds."""
    handle = _handles.get(slug)
    if handle is None:
        return
    try:
        handle.proc.terminate()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(handle.proc.wait(), timeout=grace)
    except asyncio.TimeoutError:
        try:
            handle.proc.kill()
        except ProcessLookupError:
            pass
        await handle.proc.wait()


async def fire_once(slug: str, spec: dict) -> None:
    """Launch a transient, unsupervised subprocess for a cron fire.

    Cron services' per-fire subprocesses don't go through `start()` —
    we don't want to retain them as the proc's primary handle. They run,
    their output is tee'd into log.md, and their exit code is logged.
    The parent proc stays `running`.
    """
    run = spec.get("run")
    if not run:
        return
    cmd = _spawn_args(run)

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(P.HOME_DIR),
    )
    P.append_log(slug, f"kernel: cron fire pid={proc.pid} ({run})")

    asyncio.create_task(_tee_stream(proc.stdout, slug, "stdout"), name=f"cron-out-{slug}-{proc.pid}")
    asyncio.create_task(_tee_stream(proc.stderr, slug, "stderr"), name=f"cron-err-{slug}-{proc.pid}")

    async def _log_exit() -> None:
        rc = await proc.wait()
        try:
            P.append_log(slug, f"kernel: cron fire rc={rc}")
        except P.ProcessNotFound:
            return
        # Announce the fire so PAI can surface it to the owner (unless the
        # spec explicitly opts out with `announce: false`).
        if spec.get("announce", True):
            payload = {
                "source": "kernel",
                "kind": "cron_fired",
                "slug": slug,
                "rc": rc,
            }
            if "parent" in spec:
                payload["parent"] = spec["parent"]
            P.emit_event(payload)

    asyncio.create_task(_log_exit(), name=f"cron-wait-{slug}-{proc.pid}")


async def resume_from_disk() -> None:
    """Boot-resume: for every running proc with `run:`, re-fork or fail.

    - restart=always|on-failure → re-fork (kernel death counts as failure).
    - restart=never             → mark failed with a log line.
    Cron services (have `schedule:`) are left alone here; per-fire launches
    are handled by the timer path, not boot-resume.
    """
    for slug in P.list_procs(status_filter="running"):
        try:
            spec = P.read_spec(slug)
        except P.ProcessNotFound:
            continue
        if "run" not in spec or "schedule" in spec:
            continue
        restart = spec.get("restart", "never")
        if restart in {"always", "on-failure"}:
            P.append_log(slug, f"kernel: resume-from-disk, re-forking ({restart})")
            await start(slug, spec)
        else:
            P.append_log(slug, "kernel: interrupted by kernel restart (restart=never)")
            P.resolve(slug, "failed")


async def shutdown() -> None:
    """Terminate every tracked subprocess. Called on kernel exit."""
    for slug in list(_handles.keys()):
        await stop(slug, grace=2.0)


def is_tracked(slug: str) -> bool:
    return slug in _handles
