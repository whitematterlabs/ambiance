# PAI Boot Process & `src/` Decomposition

**Date:** 2026-04-28
**Status:** Design
**Supersedes:** N/A — first concrete plan for the v3 boot architecture
**Related:** `src/guides/FILESYSTEM_v3.md` (the on-disk target)

## Goal

Move PAI from a Python-package shape (`src/` is the runtime) to a
self-contained quasi-Linux filesystem at `~/.pai/`. After install,
the agent's whole world — code, config, state, runtime — lives
under `~/.pai/`. The repo at `/Users/arda/Projects/pai/` is build
input; it is not what runs.

This spec covers two coupled changes:

1. A real boot process: `~/.pai/sbin/init` as PID 1 entrypoint,
   with `~/.pai/boot/` holding the kernel source code.
2. Decomposing today's monolithic `src/` into FHS slots under
   `~/.pai/`.

The two are coupled because the boot architecture decides where
the kernel code lives, which is the single biggest piece of `src/`.

## Non-goals

- Capability enforcement / multi-PAI jailing (deferred per v3).
- `paiman` install path against `/opt/<pkg>/<ver>/` (deferred —
  dev path lands first via `/usr/lib/pais/`).
- `/boot/recovery/` snapshots (deferred).
- Hot-reloading kernel modules.
- Replacing `paictl`'s existing CLI surface — only its plumbing
  changes when `/proc/<pid>/` + `/run/pais/<name>/` lands.

## Architectural decisions

### 1. Kernel runs as PID 1; kernelPAI is supervised

- The **kernel** is pure Python supervisor code with no LLM. It
  loads from `~/.pai/boot/`, runs the reconcile loop, spawns and
  reaps PAI processes, routes events.
- **kernelPAI** is a privileged *agent* (LLM-backed) that runs as
  one of the supervised processes, with its home at `~/.pai/root/`.
  It mediates kernel state by sending events
  (`kernel:reload_config`, etc.); it does not run the supervise
  loop itself.
- Rationale: a crash in kernelPAI's reasoning loop must not take
  the system down. Keep the supervisor dumb and reliable; keep the
  agent smart and restartable.

### 2. `/sbin/init` is a thin shim that becomes the kernel via `exec`

- `/sbin/init` verifies layout (required dirs exist), then
  `exec`s into the kernel. After exec, PID 1 *is* the kernel —
  there is no separate init process.
- Boot phases (driver probe, reconcile, start fleet) are the
  kernel's first-phase startup, not a pre-handoff script.
- Matches Linux: `/sbin/init` *is* systemd; it does not fork it.

### 3. Two-layer process view: `/proc/<pid>/` + `/run/pais/<name>/`

| Path | Keyed by | Holds | Lifetime |
|---|---|---|---|
| `~/.pai/proc/<pid>/` | PID | every running PAI process — declared, peer, or transient subagent. `status`, `cmdline`, `fd/`, current-session log. | process lifetime |
| `~/.pai/run/pais/<name>/` | name | declared/long-lived PAIs only. `current → /proc/<pid>/`, `pid`, `inbox/`, `spec.yaml`, `status`, `log.md` (durable). | across restarts |

Subagents and other transient processes get `/proc/<pid>/` entries
but no `/run/pais/<name>/` entry. "Addressable by name" is an
explicit privilege granted at `paiadd` time, not something arbitrary
spawns can claim. Name validation/sanitization happens once, in
`paiadd`.

### 4. Boot sequence (7 phases)

Run inside the kernel after `/sbin/init` execs in:

1. **Sanity check** — verify required dirs exist (`etc/`,
   `var/lib/`, `proc/`, `run/`); bail loudly if not.
2. **Clean ephemeral state** — wipe `/tmp/`, `/run/pais/<name>/current`
   symlinks, stale `/proc/<pid>/` dirs from prior boots.
3. **Driver probe** — for each driver in `/etc/drivers/`, run a
   `health()` check (paths exist, deps importable, credentials
   present). Log to `/var/log/kernel/boot.log`.
4. **Reconcile fleet** — read `/etc/config.yaml`, populate
   `/run/pais/<name>/` for each declared PAI (registered, not
   started).
5. **Start kernelPAI first** — privileged agent must be up
   before peers can escalate to it.
6. **Start fleet** — spawn each remaining PAI per its restart
   policy.
7. **Enter supervise loop** — watch `/proc/`, reap dead
   processes, route events from `/var/spool/events/`, handle
   `kernel:reload_config`.

### 5. `src/` decomposition

| Source (today) | Destination |
|---|---|
| `src/pai.py` | `~/.pai/sbin/init` (refactored into kernel entrypoint) |
| `src/kernel/` | `~/.pai/boot/` |
| `src/drivers/<name>/events.yaml` | `~/.pai/etc/drivers/<name>/` |
| `src/drivers/<name>/` (code) | `~/.pai/usr/lib/drivers/<name>/` |
| (driver runtime state) | `~/.pai/sys/drivers/<name>/` |
| `src/bin/` | `~/.pai/usr/bin/` (PAI-callable tools) |
| `src/tui/` | `~/.pai/sbin/` (privileged owner client) |
| `src/migrate.py` | `~/.pai/sbin/` |
| `src/reset.py` | `~/.pai/sbin/` |
| `src/prompts/` | `~/.pai/usr/share/prompts/` |
| `src/usr/share/doc/` | `~/.pai/usr/share/doc/` |
| `src/seed/` | *removed* — fold into bundle `defaults/` |

## Component contracts

### `~/.pai/sbin/init`

- **Inputs:** none (or environment overrides for layout root).
- **Behavior:** verify `~/.pai/` skeleton, locate kernel entry
  module under `~/.pai/boot/`, `os.execvp` into it.
- **Postcondition:** the calling process has been replaced by the
  kernel; on success, this script does not return.

### Kernel (`~/.pai/boot/`)

Modules (one responsibility each):

- `boot/entry.py` — invoked by `init`. Runs phases 1–6, then enters
  the supervise loop in phase 7.
- `boot/phases/sanity.py` — phase 1.
- `boot/phases/clean.py` — phase 2.
- `boot/phases/probe.py` — phase 3 (driver health probe).
- `boot/phases/reconcile.py` — phase 4 (config → `/run/pais/`).
- `boot/phases/start.py` — phases 5–6 (spawn PAIs).
- `boot/supervise.py` — phase 7 main loop.
- `boot/router.py` — event routing.
- `boot/proc.py` — PID-keyed `/proc/<pid>/` management.
- `boot/run.py` — name-keyed `/run/pais/<name>/` management.

### `paictl`

Existing CLI verbs (`start`, `stop`, `restart`, `status`, `logs`)
keep their shapes. Internals change:

- `start <name>` resolves through `/run/pais/<name>/`, then
  spawns and stamps `/proc/<pid>/`, links `current → /proc/<pid>/`.
- `status <name>` reads `/run/pais/<name>/status` and follows
  `current` for live process info.
- `stop <name>` reads `pid`, signals, waits, removes `/proc/<pid>/`.

### Drivers

Three-way split codified:

- `~/.pai/etc/drivers/<name>/events.yaml` — config (event manifest).
- `~/.pai/usr/lib/drivers/<name>/` — code (Python module).
- `~/.pai/sys/drivers/<name>/` — runtime state (cursors, last event).

Driver code imports nothing from `etc/` or `sys/` directly; the
kernel passes paths in at construction.

## Migration strategy

The repo retains `src/` as build input. Install lays the repo's
contents into `~/.pai/` slots. For the dev path:

- `src/` is rearranged in-repo to mirror the FHS slots that house
  *code* (so `src/boot/`, `src/usr/lib/drivers/<name>/`,
  `src/usr/bin/`, `src/sbin/`, `src/usr/share/`).
- An install script (`scripts/install.sh` or `paiman init` later)
  symlinks or copies the repo's slot trees to `~/.pai/`.
- State-bearing slots (`~/.pai/var/`, `~/.pai/etc/`, `~/.pai/proc/`,
  `~/.pai/run/`, `~/.pai/sys/`, `~/.pai/home/`, `~/.pai/root/`,
  `~/.pai/tmp/`, `~/.pai/boot/recovery/`) are created empty by the
  install script, never sourced from the repo.

The exact symlink-vs-copy choice is the v3 open question; this
spec keeps it open and assumes the install script encapsulates it.

## Testing

- **Unit:** each phase module tested in isolation against a temp
  `~/.pai/`-shaped directory.
- **Integration:** `tests/conftest.py` already builds a temp PAI
  root; extend it to lay out the FHS skeleton, then drive
  `boot.entry.main()` end-to-end and assert phases run in order.
- **Smoke:** a `tests/test_boot_smoke.py` that runs init against a
  minimal `etc/config.yaml` (one trivial PAI) and asserts
  `/run/pais/<name>/status == running`.

## Risks / open questions

- **Install mechanism (symlink vs copy)** is still open per v3.
  This spec is agnostic; the install script encapsulates it.
- **Where does the Python venv live during dev?** Today
  `~/.pai/usr/lib/venv/` per v3, but `uv sync` against the repo
  produces `.venv` at the repo root. Bridging is an implementation
  detail for the install script.
- **Subagent process accounting.** Subagents are children of a
  PAI. Do they get their own `/proc/<pid>/` entry, or do we only
  surface the PAI's root PID? Spec says yes, they get entries —
  but the kernel doesn't *supervise* them; their parent does.
- **`tui/` in `/sbin/` vs `/bin/`.** The owner's TUI is privileged
  (it talks directly to kernel surfaces). v3's split is
  privilege-based, so `/sbin/` is the principled answer; revisit
  if non-privileged TUI variants emerge.

## Out of scope (this spec only)

- Implementation plan (sequencing, file-by-file diff). That's
  the next artifact, produced by `writing-plans`.
- Documentation rewrites beyond `FILESYSTEM_v3.md` updates.
- `paictl`'s UX revamp.
