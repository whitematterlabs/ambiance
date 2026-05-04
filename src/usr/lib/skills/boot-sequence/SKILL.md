---
name: boot-sequence
description: What happens from /sbin/init through reconcile to the first nudge — phases, ordering, what each phase touches. Read when diagnosing a boot-time failure.
---

# Boot sequence

The kernel runs as PID 1. From cold start to first nudge:

## 0. Provisioning (one-time, dev only)

`paifs-init` lays down the FHS skeleton at `~/.pai/`:
- creates dirs (`/etc`, `/usr`, `/var`, `/proc`, `/run`, `/sys`, `/home`, `/root`, …)
- symlinks `/usr/src/`, `/usr/lib/drivers/`, `/usr/share/prompts/`,
  `memory/doc/` at the live repo
- builds a self-contained venv at `/usr/lib/venv/`
- generates console-script shims at `/usr/bin/`

Run once per fresh checkout. Idempotent.

## 1. Entry — `/sbin/init`

Tiny entrypoint that `exec`s into the kernel:

```sh
exec /usr/bin/python -m boot run
```

From the repo (dev): `uv run python -m boot run`.

## 2. Bootstrap — `/usr/src/boot/bootstrap.py`

The main coordinator. Calls each phase in order and starts the
event loop. See `src/boot/bootstrap.py` for the canonical sequence.

## 3. Phases — `/usr/src/boot/phases/`

Each phase has a single responsibility:

| Phase | File | Job |
|---|---|---|
| **probe** | `probe.py` | Sanity-check the FHS layout: required dirs, drivers, prompts. Refuse to boot if structure is missing. |
| **clean** | `clean.py` | Drain stale `/run/pai/events/`; clear any half-written transient state. |
| **reconcile** | `reconcile.py` | Read `/etc/config.yaml`. For each entry: rewrite managed fields onto `/proc/<pai>/spec.yaml`; reconcile `dependencies:` (persubs); mark missing entries. |
| **sanity** | `sanity.py` | Cross-check post-reconcile: every `wake_on` glob references a real driver kind; no pid collisions. |
| **start** | `start.py` | Spawn supervisors for every active PAI in `/proc/`. |

After phases: enter the kernel main loop (FS watcher + timer heap;
see skill `understand-kernel`).

## 4. Recovery

`/usr/src/boot/recovery/` handles "kernel crashed mid-flight" cases.
On boot, any PAI process whose `restart` policy is `never` and whose
`status` was `running` is marked `failed` with a one-line log entry —
the operator decides whether to restart. `on-failure` and `always`
PAIs are resumed automatically.

## 5. First nudge

After `start`, the kernel sleeps until either:
- A driver writes the first event under `/run/pai/events/`, or
- A timer in the heap fires.

Whichever comes first wakes the kernel and produces the first nudge.

## Common boot failures

- **Schema error in `/etc/config.yaml`** — reconcile raises; kernel
  emits `kernel:reload_failed` and continues with the previous good
  spec. root receives the event and runs the `reload-config` skill.
- **Missing driver kind referenced in `wake_on`** — sanity phase
  surfaces it; usually a typo or a removed driver.
- **Pid collision** — reconcile refuses to assign; root surfaces.

## Read these next

- `/usr/src/boot/bootstrap.py` — the sequence in code.
- `/usr/src/boot/phases/reconcile.py` — the reconcile pass.
- Skill `understand-config-reconcile` — what reconcile manages.
- Skill `understand-kernel` — the post-boot main loop.
