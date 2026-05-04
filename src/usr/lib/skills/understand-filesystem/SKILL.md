---
name: understand-filesystem
description: The FHS map — what each top-level directory means, what's kernel vs userspace, where source vs runtime state lives. Read before touching unfamiliar paths.
---

# FHS layout (v3)

Authoritative spec: `memory/doc/FILESYSTEM_v3.md`. This skill
is the cheat sheet.

## The layering rule (load-bearing)

| Path | Holds | Repo source |
|---|---|---|
| `/boot/` | **Kernel image.** PID 1 supervisor + every helper it links against. Pure Python. | `src/boot/` |
| `/sbin/` | KernelPAI / owner-only tools that mutate `/etc/`, the fleet, or system state: `init` (entrypoint), `paiman`, `paiadd`, `paidel`, `paifs-init`, `migrate`, `reset`, `tui`. | `src/sbin/` + privileged shims from `src/bin/` |
| `/bin/`, `/usr/bin/` | PAI-callable tools (`paictl`, `paicron`, `ipc`, `subagent`, …). `/bin/` is a symlink to `usr/bin/`. | `src/bin/`, generated shims |
| `/usr/` | Userspace. Drivers, skills, PAI bundles, shipped data. **Never kernel code.** | `src/usr/` (kernel docs only) + `~/Projects/pairegistry/` (drivers, skills, libs, pais, prompts — installed via `paiman install <name>`) |

Kernel code never lives under `/usr/`. Userspace never lives under
`/boot/`. If something owns the on-disk shape of an external surface
(messages, email, contacts), it is a **driver**, not kernel.

## Userspace breakdown

- `/usr/lib/drivers/<name>/` — driver source + `events.yaml`.
- `memory/skills/<name>/` — skill source (this file lives here).
- `/usr/lib/pais/<name>/` — in-development PAI bundle source.
- `/usr/lib/venv/` — the Python virtualenv all PAIs share.
- `/usr/share/prompts/` — shipped baseline prompts.
- `memory/doc/` — shipped documentation.
- `/usr/src/` — userspace Python libs used by drivers/skills/bundles.
  *Kernel code is NOT here* — kernel is `/boot/`.

## State

- `/etc/config.yaml` — fleet declaration. **Source of truth.**
- `/var/lib/instances/<pai>/` — per-PAI sacred state (memory,
  workspace, inbox). Survives uninstall.
- `/var/lib/memory/` — shared canonical memory.
- `/var/log/` — append-only logs.
- `/var/spool/communication/messages/` — message queues.
- `/sys/drivers/<name>/` — driver runtime state (cursors, last event).
- `/proc/<slug>/` — kernel-managed process lifecycle (status, log,
  spec, pid).
- `/run/pai/events/` — kernel inbox; events consumed on read.
- `/tmp/` — system-wide ephemeral. Per-PAI ephemerals go in
  `/home/<pai>/tmp/`.

## Homes

- `/root/` — pid 1 (root) home. Stitched view of root's instance.
- `/home/<pai>/` — every other PAI. Symlinks back into
  `/var/lib/instances/<pai>/` and `/var/lib/memory/`.

## Three-location driver split

Every driver fans across three slots — keep them straight:

| Slot | Holds |
|---|---|
| `/usr/lib/drivers/<name>/` | code + `events.yaml` |
| `/sys/drivers/<name>/` | live runtime state |
| `/proc/<slug>/` | lifecycle (slug may be `<name>-in`/`<name>-out`) |

There is **no `/etc/drivers/`**. Drivers are a code-time registry
in the kernel (see `DRIVER_SPECS` in `/usr/src/boot/main.py`).

## When in doubt

`memory/doc/FILESYSTEM_v3.md` overrides anything that drifts.
