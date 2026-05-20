# Kernel Architecture (v3)

The authoritative spec is `FILESYSTEM_v3.md`. This is the operator's
quick map of the kernel itself — what runs, who owns what, how events
flow.

## Layering rule (load-bearing)

- **`/boot/`** — kernel image. PID 1's supervisor + every helper it
  links against. Pure Python, no LLM. Repo source: `src/boot/`.
- **`/usr/`** — userspace. Drivers, skills, PAI bundles, shipped data.
  Never holds kernel code.
- **`/sbin/`** — kernelPAI / owner-only tools + `/sbin/init` (entrypoint).
- **`/bin/`** (relative symlink to `usr/bin/`) — PAI-callable tools
  (`paictl`, `paicron`, `send-message`, `subagent`, …).

If something owns the on-disk shape of an external surface (messages,
email, calendar, contacts), it is a **driver**, not kernel.

## What the kernel actually is

The kernel is one OS process running as PID 1. It is *not* a fleet of
workers. Inside that single process, three concerns are interleaved on
one asyncio event loop:

1. **Event loop + filesystem watcher.** A tickless supervise loop sleeps
   on whichever fires first: a new event file under `/var/spool/events/`
   (via `EventWatcher` in `events.py`) or the next pending timer
   (`timers.py`). When the heap is empty and no events are pending, it
   blocks indefinitely on the watcher.
2. **In-process driver tasks.** Drivers are asyncio coroutines living
   inside the kernel itself — *not* child processes. The kernel
   discovers, schedules, supervises, and cancels them.
3. **PAI process supervision.** PAIs are real OS subprocesses spawned
   via `asyncio.create_subprocess_exec` (see `supervisor.py`). The
   kernel watches them through `/proc/<pai>/` and reaps on exit.

These two things — driver tasks and PAI processes — are **not the
same shape**. The shared `/proc/<slug>/` directory is a uniform
introspection surface, but the lifecycle underneath is different.

## Drivers: in-process asyncio tasks

Drivers own the on-disk shape of external surfaces (iMessage, email,
calendar, voice, …). Source lives in `~/Projects/pairegistry/drivers/<name>/`
and is installed into `/usr/lib/drivers/<name>/` by `paiman install`.
There is no `/etc/drivers/` — drivers are a code-time registry, not
user-editable config.

### Discovery: `DRIVER_SPECS`

At kernel boot, `_discover_driver_specs()` walks every `events.yaml`
under `/usr/lib/drivers/` (recursing through symlinks to support
sub-driver namespaces like `email/macmail/`). For each manifest with a
`processes:` section, it records `(slug, factory)` where `factory()`
imports the module and calls the named entrypoint to return a coroutine.
The result is the module-level `DRIVER_SPECS` tuple.

`paiman install` and `paiman remove` change the on-disk set;
`_reconcile_drivers()` re-discovers on every call, so install/remove
takes effect on `kernel:reload_config` without a kernel restart.

### The runtime contract

A driver entrypoint **must be `async def`**. A sync `def run()` with
`while True: time.sleep(N)` enters its loop on the kernel's main thread
when reconcile calls it, never returns, and wedges every other driver
and every PAI nudge until the kernel is killed. Blocking I/O inside
an async driver (sync `requests`, `subprocess.run`, blocking `sqlite3`)
freezes the event loop for the duration of the call — wrap such calls
in `asyncio.to_thread`, or use the asyncio-native equivalents.

Driver tasks are cancelled on shutdown and on `paictl stop <slug>`.
Honor `asyncio.CancelledError` for cleanup.

See the `author-driver` skill for the full contract and reference
skeletons.

### `active:` flag and reconcile

Each driver gets a `/proc/<slug>/` entry written by `_ensure_driver_proc`
on first spawn: `{ kind: driver, active: true }`. The `active:` flag
(read by `_driver_active`) decides whether the coroutine should be
running. `paictl start/stop <slug>` flips it and emits
`kernel:reload_config`; the kernel's `_handle_reload_config` calls
`_reconcile_drivers()` which:

- Spawns drivers that should run but aren't (creates an `asyncio.Task`
  wrapping `_supervise_driver(slug, coro)`, records it in `_driver_tasks`).
- Cancels and awaits drivers that are running but shouldn't be.
- Drops tasks for slugs that no longer exist in `DRIVER_SPECS`
  (driver was uninstalled).
- GCs any task whose coroutine has finished — so a respawn after a
  crash or stale cancellation can distinguish "still running" from
  "long dead."

Reconcile is **event-driven, never polled**. It runs once at boot and
on every `kernel:reload_config`.

`_supervise_driver` resolves `/proc/<slug>/status` to `cancelled` on
clean shutdown (no nudge) and `failed` on crash (writes traceback to
`log.md` and nudges via the standard `proc_resolved` path).

### Driver split

| Slot | Holds |
|---|---|
| `/usr/lib/drivers/<name>/` | Source code + shipped `events.yaml` manifest |
| `/sys/drivers/<name>/` | Driver-internal runtime state (cursors, last event) |
| `/proc/<slug>/` | Kernel-managed lifecycle (`spec.yaml` with `active:`, `status`, `log.md`) |

## PAIs: supervised subprocesses

A PAI is a real OS subprocess — not a coroutine. `boot/supervisor.py`
owns the lifecycle:

- `start(slug, spec)` — `asyncio.create_subprocess_exec` on the PAI's
  `run:` command, tees stdout/stderr into `/proc/<slug>/log.md`,
  records the live OS pid into `/proc/<slug>/spec.yaml`.
- `_await_exit(slug)` — awaits the subprocess; resolves
  `/proc/<slug>/status` and emits a `proc_resolved` event so the
  parent PAI (if any) gets nudged.
- `stop(slug, grace)` — SIGTERM, then SIGKILL after grace.
- `fire_once(slug, spec)` — transient one-shot (cron tick that has a
  `run:`).
- `resume_from_disk()` — at boot, reattach to anything `/proc/`
  claims is running (or mark it failed if the pid is gone).

PAIs are declared in `/etc/config.yaml`. `paictl start/stop <name>`
flips `active:` there and emits `kernel:reload_config`, the same event
drivers use. `_handle_reload_config` runs `_reconcile_drivers()`
*first* (driver reconcile must not wait on per-PAI locks held by
in-flight nudges from a runaway driver), then drains PAI locks and
calls `C.reconcile_from_config()` to spawn/stop PAI processes and
re-stitch `/home/<pai>/`.

### Why drivers aren't subprocesses

Drivers share kernel-internal state (the routing table, the event
queue, contacts/messages helpers). Forking them out would mean an IPC
boundary for every event. They are I/O-bound by nature (waiting on
sockets, files, APIs), which is exactly what asyncio is for. The
trade-off is the async contract above: a blocking driver wedges the
loop, so the contract is non-negotiable.

PAIs, by contrast, run an LLM tool loop, hold their own working
memory, and must be isolatable for crashes and restarts — subprocess
is the right boundary.

## Event flow

1. Anything (a driver, a PAI's `send-message`, a cron tick, the TUI,
   `/sbin/reboot`) writes an event file under `/var/spool/events/`
   with a `kind:` field.
2. `EventWatcher` picks it up; the supervise loop calls
   `_handle_event_file`.
3. Kernel-shaped kinds (`kernel:reload_config`, `kernel:restart`,
   `interrupt`, …) are handled inline.
4. Driver-shaped kinds (`imessage:new`, `email:new`, … or any
   `<source>:<kind>` from a generic driver) get routed by
   `_route_to_pids`:
   - Fan-out to every PAI whose `wake_on:` glob matches the kind.
   - Zero matches → every PAI with `fallback: true`.
   - Still zero → root (pid 1).
5. Each matched PAI is nudged via `_dispatch_nudge`, which serializes
   per-PAI under `_pai_locks[pid]` so concurrent events don't race on
   `messages.jsonl`. Nudges are cancellable asyncio tasks tracked in
   `_active_nudges[pid]`; an `interrupt` event cancels them.

The kernel does not know what a "message" is. On-disk shape decisions
belong to drivers.

## Reserved PIDs

- `1` → `root` — kernel-internal events, errored nudges, fallback.
- `2` → `pai` — owner-facing PAI, catch-all.

Auto-allocated PIDs are invariant once assigned.

## Restart in place

`/sbin/reboot` emits `kernel:restart`. `_handle_restart` drains
in-flight nudges (bounded timeout, default 5s — a runaway driver must
not block reboot on the very thing being restarted), then raises
`_RestartRequested`. `run()`'s `finally` block runs the orderly
shutdown (cancel drivers, cancel nudges, resolve non-cron procs, reap
the pgrp + descendants), and `entry.py` catches the global
`_restart_requested` flag and `os.execvp`s the kernel binary in place
— PID 1 preserved.

## Source-of-truth files

- `/etc/config.yaml` — fleet declaration. Reconcile rewrites
  `/proc/<pai>/spec.yaml` from it on boot and on `kernel:reload_config`.
- `/usr/lib/drivers/<name>/events.yaml` — every kind a driver may
  emit, plus its `processes:` (slug, module, entrypoint). Routing
  vocabulary + kernel-internal driver registry source.
- `boot/config.py` → `CONFIG_MANAGED_FIELDS` — schema authority for
  what reconcile manages vs. preserves on `spec.yaml`.

## `/proc/<slug>/` (uniform surface)

Both drivers and PAIs share the layout:

- `spec.yaml` — for PAIs: last reconciled spec (managed fields
  rewritten, others preserved). For drivers:
  `{ kind: driver, active: true|false }`.
- `pid` — POSIX pid (PAIs only; drivers don't have one — they're
  asyncio tasks inside the kernel).
- `status` — `running` / `failed` / `stopped` / `cancelled` /
  `expired` / `completed`.
- `log.md` — append-only operational log (tracebacks land here).

The shared layout is what makes `paictl status` and `paictl logs`
work the same way for both. The lifecycle underneath is different;
the introspection surface is not.
