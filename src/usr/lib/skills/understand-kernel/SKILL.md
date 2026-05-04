---
name: understand-kernel
description: Read first — what the PAI kernel is, why it's tickless, and what it does. Foundation for every other root skill.
---

# What the kernel is

PAI is an LLM — it only thinks when prompted. The kernel is the
nudge mechanism: it sleeps, wakes on events or timers, builds
context, and prompts the right PAI into action. Without the kernel,
PAI only responds when a human talks to it. With it, PAI is an
always-on agent.

The kernel is **PID 1** (a pure-Python supervisor) running from
`/boot/`. Source: `/usr/src/boot/` (symlinked from the repo's
`src/boot/`).

## Tickless

No polling, no system cron. The kernel sleeps until **whichever
fires first**:

1. **FS event** — a file lands in `/run/pai/events/` (watchdog →
   FSEvents/kqueue). Drivers drop events here.
2. **Timer expiry** — `proc/<slug>/spec.yaml` has a `deadline:` or
   `schedule:`; the kernel keeps a min-heap of fire times and sleeps
   until the earliest.

Zero CPU between wakeups. See `/usr/src/boot/main.py` for the
loop and `/usr/src/boot/timers.py` for the heap.

## Responsibilities

1. **Track human plans** as `/proc/<slug>/` entries with deadlines.
2. **Run PAI's own jobs** (cron) — consolidation sweeps, periodic
   check-ins. Kernel-driven, not system crontab.
3. **Listen for notifications** — driver events route to PAIs.
4. **Track subagents** — children write back via the event bus.
5. **Reconcile** `/etc/config.yaml` against `/proc/<pai>/spec.yaml`
   on boot and on `kernel:reload_config`.
6. **Supervise** PAI processes — start, restart per policy, mark
   failed, write status to `/proc/<pai>/status`.

## What the kernel does NOT do

- It does not know what a "message" is. On-disk shape decisions
  belong to **drivers**.
- It does not edit user state. PAIs do.
- It does not interpret event payloads — it routes by `kind:`.

## Read these next

- `memory/doc/KERNEL.md` — the long-form tour (philosophy,
  loop, event/timer/resolve flows, spawning).
- `memory/doc/KERNEL_ARCHITECTURE.md` — operator's quick map.
- Skill `understand-event-routing` — how a `kind` becomes a nudge.
- Skill `understand-filesystem` — where everything lives.
