# Self-Healing

How PAI keeps itself running. What restarts what, when, and how the
system survives crashes.

The system has three failure domains, and they recover differently
because they are different kinds of process:

1. **The kernel** (PID 1, pure Python — `src/boot/main.py`). The
   supervisor itself. No LLM. If it dies, *everything* it was
   supervising dies with it.
2. **PAI processes** (`/proc/<pai>/`). LLM-driven agents — including
   kernelPAI (the privileged agent at `/root/`). KernelPAI is **not**
   the supervisor; it is supervised like any other PAI.
3. **Drivers** (`/proc/<slug>/`, `restart:` policy in `spec.yaml`).
   Background subprocesses spawned from `/usr/lib/drivers/<name>/`.

The kernel survives the other two by being separate from them. A
crash in kernelPAI's reasoning loop, or in the gmail driver, or in
any peer PAI, does not take the system down.

## Domain 1 — The kernel

The kernel does not self-restart on crash. There is no watchdog
above PID 1 inside `~/.pai`. If `src/boot/main.py` raises an
uncaught exception, the process exits and the OS reaps it; whatever
started it (a TTY shell, `launchctl`, `PAI.app`) is responsible for
respawning. That outer layer is intentionally thin and outside the
kernel's contract.

### Liveness probe

`run/kernel.pid` is the kernel's lock file. The kernel holds an
exclusive `flock` on it for the lifetime of the process. Anything
that wants to know "is a kernel running?" attempts a non-blocking
`flock`:

- grabbed → no kernel is up (release immediately)
- `BlockingIOError` → a kernel holds it; read the file for its PID

`/sbin/reboot` uses this probe before emitting anything. If no
kernel is running, it bails with a hint to run `init`.

### In-place re-exec (`kernel:restart`)

The kernel can re-exec itself **without losing PID 1**. This is
how on-disk patches to kernel-imported modules (anything under
`/boot/` or `/usr/src/`) get loaded.

The path:

1. `/sbin/reboot` emits `kernel:restart` via the event spool.
2. `main.run()` receives it and calls `_handle_restart()`:
   drains in-flight nudges (per-PAI locks) with a 5-second bounded
   timeout — a runaway driver must not block the very restart that
   would clear it, so drain is best-effort.
3. Sets `_restart_requested = True`, raises `_RestartRequested`.
4. The exception bubbles through `run()`'s `finally`, which runs
   the orderly shutdown: cancels active nudges, stops drivers,
   resolves running procs (cron services with a `schedule:` are
   left running by design — `rebuild_from_proc` re-arms them on
   the next boot).
5. `entry.py` reads `_restart_requested` after `asyncio.run()`
   returns and `os.execvp`s the same argv `/sbin/init` uses. PID 1
   is preserved.

The same path runs whether the trigger is `/sbin/reboot`, the
`PAI.app` menu, or a kernelPAI skill that emits `kernel:restart`
directly.

## Domain 2 — PAI processes

PAIs (peers, kernelPAI, declared fleet members in `/etc/config.yaml`)
are tracked by the supervisor in `src/boot/supervisor.py`. Each
proc carries a `restart:` field in its `/proc/<slug>/spec.yaml`:

- `never` (default) — exit is terminal; status flips to
  `completed` (rc=0) or `failed` (rc≠0).
- `on-failure` — re-fork only if rc ≠ 0.
- `always` — re-fork on any exit.

`_await_exit` watches the subprocess. On exit it consults the
spec, logs the rc to `/proc/<slug>/log.md`, and either calls
`start()` again (re-fork, same spec) or `P.resolve()` to mark
terminal state.

**There is no exponential backoff.** Restart is immediate. A PAI
that crashes on boot will spin until something else intervenes
(operator stops it, kernel restarts, fix lands). The log line
trail in `/proc/<slug>/log.md` is the only rate limiter.

On kernel restart, `supervisor.resume_from_disk()` walks `/proc/`
and re-forks any proc whose `restart:` is `always` or `on-failure`
— kernel death counts as failure. `restart: never` procs get a
log line ("interrupted by kernel restart") and stay resolved.

### Conversation-level recovery (context overflow)

Domain 2 also covers a failure that is *not* a crash: a PAI's
history growing past the provider's hard context limit. Once that
happens every nudge 400s — and the soft `bin/compact` path can't
help, because the compaction turn carries the same oversized
history. Worse, each failed nudge used to re-nudge kernelPAI,
snowballing into a backlog of cancelled nudges and a cascade of
connection/timeout errors (the "nudge-failure storm").

`src/boot/nudge.py` handles both:

- **Reactive overflow recovery.** On an *observed* overflow it
  archives the oversized history to `*-overflow.jsonl`, resets the
  conversation, and retries the turn once. It self-calibrates to the
  real provider limit (it fires only on an observed overflow, never a
  guessed token count) and needs no cooperation from the model.
- **Escalation gating.** Transient/systemic errors (connection,
  timeout, rate limit, overflow) are logged and dropped instead of
  re-nudging kernelPAI per failure. Only genuine, actionable
  failures still escalate.

## Domain 3 — Drivers

Drivers live in `/proc/<slug>/` like PAIs and use the same
`restart:` policy plumbing. The differences are upstream:

- Driver specs are derived from `/usr/lib/drivers/<name>/` (code +
  shipped `events.yaml`) and the `active:` flag on
  `/proc/<slug>/spec.yaml`.
- `paictl start/stop <slug>` flips `active:` and emits
  `kernel:reload_config`. The kernel's reconcile path
  (`_handle_reload_config` → `_reconcile_drivers`) starts or stops
  the driver subprocess. **This is the path for restarting a
  driver: stop + start, not in-process recycle.**
- Driver reconcile runs *before* the per-PAI nudge drain on
  `kernel:reload_config`, because a runaway driver can outrun the
  drain (every event wakes a PAI). Order matters.

A crashed driver follows the same `_await_exit` path as a PAI —
the `restart:` field decides. Most drivers ship `restart: always`.

## Tool boundary: `paictl` vs `paicron`

These do different things; do not confuse them:

- **`paictl`** — fleet runtime. Operates at PAI and driver
  granularity by flipping `active:` on a fleet entry or driver
  spec, then emitting `kernel:reload_config`. The reconcile loop
  does the spawning/stopping. `paictl` has no `restart` verb.
- **`paicron`** — services (cron jobs, watchers, async work).
  Owns `/proc/<svc>/` entries that are not PAIs. `paicron restart
  <slug>` cancels and re-spawns with the same spec.

If the thing crashed is a PAI or a driver, the recovery surface is
`paictl` (stop, then start). If it is a cron-style service, the
surface is `paicron restart`.

## Triage order when something is wrong

1. **Probe the kernel.** Does `run/kernel.pid` exist *and* is its
   `flock` held? If not, no kernel is up — start one. Until the
   kernel is back, nothing else can recover.
2. **Classify the failure.** Read `/proc/<slug>/status` and the
   tail of `/proc/<slug>/log.md`. PAI crash, driver crash, kernel
   issue?
3. **Apply the matching tool.**
   - PAI or driver subprocess died with `restart: never` → decide
     whether to flip it or accept the resolved state.
   - On-disk patch to kernel-imported code needs to load →
     `/sbin/reboot` (or the `kernel-restart` skill in
     pairegistry, which wraps it).
   - Driver behaving badly → `paictl stop <slug>` then `paictl
     start <slug>`.
   - Cron service stuck → `paicron restart <slug>`.
   - Config-shaped problem → fix `/etc/config.yaml`, then
     `paictl reload` (emits `kernel:reload_config`).
   - Structural (import error, schema mismatch, missing creds) →
     surface to the operator; do not paper over it.

## Boundaries — what self-healing does not do

- **No automatic kernel restart on uncaught exception.** That is
  the outer process manager's job (launchd, the app shell, the
  TTY), not the kernel's.
- **No silent file deletion.** Investigating an error means
  reading state, not removing it.
- **No cross-instance writes.** Another PAI's
  `/var/lib/instances/<pai>/` is sacred; do not touch it from
  inside a self-heal turn.
- **No edits to kernel source from a PAI turn.** Surface, let the
  operator land the patch, then `/sbin/reboot`.

## Reference

- `FILESYSTEM_v3.md` — full FHS spec.
- `KERNEL_ARCHITECTURE.md` — layering and event routing.
- `src/boot/main.py` — supervisor, signal handlers, restart path.
- `src/boot/supervisor.py` — subprocess `restart:` policy.
- `src/sbin/reboot.py` — `kernel:restart` emitter + lock probe.
