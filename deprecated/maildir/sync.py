"""Maildir sync supervisor.

Owns the lifecycle of `mbsync` and `goimapnotify` subprocesses:

  - On startup: regenerate mbsyncrc/msmtprc/imapnotify configs from
    `/etc/mail/accounts.yaml`.
  - One `goimapnotify` subprocess per account, holding an IMAP IDLE.
    When new mail lands server-side, goimapnotify shells out to
    `mbsync <account>`. mbsync writes the file into the local Maildir;
    the inbound driver's watchdog notices and emits the kernel event.
  - A periodic `mbsync -a` sweep every `POLL_INTERVAL` as a safety net
    for accounts that drop their IDLE or land mail mbsync missed.

If `accounts.yaml` is missing or empty, the driver writes empty configs
and idles forever.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
from pathlib import Path

from boot import paths

from . import config as Cfg


POLL_INTERVAL = 5 * 60  # seconds — periodic mbsync -a sweep


def _which(cmd: str) -> str | None:
    return shutil.which(cmd)


def _imapnotify_config_path(address: str) -> Path:
    return paths.etc_mail() / f"imapnotify-{address}.json"


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    tmp.replace(path)


def _regenerate_configs(accounts: list[Cfg.Account]) -> None:
    Cfg.write_generated(accounts)
    for a in accounts:
        body = json.dumps(Cfg.render_imapnotify(a), indent=2)
        path = _imapnotify_config_path(a.address)
        _atomic_write(path, body)
        path.chmod(0o600)


async def _run_mbsync_all() -> None:
    """One full sweep of every channel."""
    mbsyncrc = paths.etc_mail() / "mbsyncrc"
    if not mbsyncrc.exists():
        return
    if _which("mbsync") is None:
        print("[maildir-sync] mbsync not on PATH", flush=True)
        return
    proc = await asyncio.create_subprocess_exec(
        "mbsync", "-c", str(mbsyncrc), "-a",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        err = stderr.decode("utf-8", errors="replace").strip()
        print(f"[maildir-sync] mbsync -a failed (exit {proc.returncode}): {err}", flush=True)


async def _periodic_sweep(stop: asyncio.Event) -> None:
    """Background task: full mbsync sweep on a fixed interval."""
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=POLL_INTERVAL)
            return
        except asyncio.TimeoutError:
            pass
        await _run_mbsync_all()


async def _supervise_imapnotify(account: Cfg.Account, stop: asyncio.Event) -> None:
    """Run goimapnotify for one account; restart with backoff on crash.

    goimapnotify is single-account per process, so we spawn one each.
    """
    if _which("goimapnotify") is None:
        print(
            f"[maildir-sync] goimapnotify not on PATH; "
            f"falling back to {POLL_INTERVAL}s polling for {account.address}",
            flush=True,
        )
        return
    cfg = _imapnotify_config_path(account.address)
    backoff = 2
    while not stop.is_set():
        proc = await asyncio.create_subprocess_exec(
            "goimapnotify", "-conf", str(cfg),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        print(f"[maildir-sync] goimapnotify started for {account.address} (pid={proc.pid})", flush=True)

        async def _wait() -> int:
            await proc.wait()
            return proc.returncode if proc.returncode is not None else -1

        wait_task = asyncio.create_task(_wait())
        stop_task = asyncio.create_task(stop.wait())
        done, pending = await asyncio.wait(
            {wait_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
        )
        for t in pending:
            t.cancel()

        if stop.is_set():
            try:
                proc.send_signal(signal.SIGTERM)
                await asyncio.wait_for(proc.wait(), timeout=5)
            except (ProcessLookupError, asyncio.TimeoutError):
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            return

        rc = wait_task.result() if wait_task.done() else -1
        err = b""
        if proc.stderr is not None:
            try:
                err = await asyncio.wait_for(proc.stderr.read(), timeout=1)
            except asyncio.TimeoutError:
                err = b""
        print(
            f"[maildir-sync] goimapnotify {account.address} exited rc={rc}: "
            f"{err.decode('utf-8', errors='replace').strip()}",
            flush=True,
        )
        try:
            await asyncio.wait_for(stop.wait(), timeout=backoff)
            return
        except asyncio.TimeoutError:
            pass
        backoff = min(backoff * 2, 60)


async def run() -> None:
    accounts = Cfg.load_accounts()
    if not accounts:
        print("[maildir-sync] no accounts configured; idle", flush=True)
        await asyncio.Event().wait()
        return

    paths.etc_mail().mkdir(parents=True, exist_ok=True)
    _regenerate_configs(accounts)
    print(
        f"[maildir-sync] generated configs for {len(accounts)} account(s)",
        flush=True,
    )

    # Initial sweep so a freshly-configured account picks up its existing
    # mail without waiting for the first IDLE notification.
    await _run_mbsync_all()

    stop = asyncio.Event()
    tasks: list[asyncio.Task] = [
        asyncio.create_task(_periodic_sweep(stop), name="maildir-sync-poll"),
    ]
    for a in accounts:
        tasks.append(asyncio.create_task(
            _supervise_imapnotify(a, stop),
            name=f"maildir-sync-idle-{a.address}",
        ))

    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        stop.set()
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        raise
    finally:
        print("[maildir-sync] stopped", flush=True)
