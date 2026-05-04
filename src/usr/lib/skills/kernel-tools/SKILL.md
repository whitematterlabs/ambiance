---
name: kernel-tools
description: Cheatsheet for paiman, paiadd/paidel, paictl, paicron, ipc, subagent — what each does, when to use which. Read before invoking unfamiliar tooling.
---

# Kernel tooling

Five command families, each scoped to one concern. Don't reach for
a different one to "make it work."

## paiman — bundle layer

Manages templates (`/opt/<pkg>/<ver>/`, `/usr/lib/pais/<name>/`).

```sh
paiman init <name>            # scaffold a new dev bundle at /usr/lib/pais/<name>/
paiman install <bundle>       # install a release bundle into /opt/
paiman uninstall <bundle>     # refused if any instance references it
paiman list                   # available bundles
```

## paiadd / paidel — instance layer

Configure / unconfigure a PAI. Wizard-style; writes
`/etc/config.yaml` and `/var/lib/instances/<name>/`.

```sh
paiadd <bundle>               # useradd-style wizard
paidel <name>                 # remove fleet entry; preserves instance state (sacred)
paidel <name> --purge         # also wipe /var/lib/instances/<name>/
```

Both end by emitting `kernel:reload_config`. **This is the supported
path for adding/removing a PAI** — hand-editing `/etc/config.yaml`
to add or remove is wrong.

## paictl — instance runtime

Start/stop fleet members already configured.

```sh
paictl start <name>           # flip /proc/<name>/spec.yaml `active: true`
paictl stop <name>             # flip to false
paictl status                 # one-line per PAI
```

Both flip the `active:` flag and emit `kernel:reload_config`.
Reconcile is event-driven, never polled.

## paicron — service layer

Spawn cron jobs, watchers, and async work — the systemctl-shaped
frontend for `/proc/<slug>/`.

```sh
# Background subagent (one-shot)
paicron start --slug research-flights \
    --run "bin/subagent 'flights to istanbul'" \
    --restart never

# Cron job (recurring)
paicron start --slug nightly-consolidation \
    --schedule "0 3 * * *" \
    --run "bin/consolidate"

# Reminder (no run:, just a timer that nudges PAI on fire)
paicron start --slug call-mom \
    --schedule "2026-05-04T18:00:00"

paicron stop <slug>           # mark cancelled
paicron list                  # one-line per /proc/ entry
```

`paicron` auto-suffixes the slug with `-YYYY-MM-DD` (or full
timestamp on same-day collision).

## ipc — event bus

```sh
# Send a directed message to another PAI
ipc --to <pid|slug> --content "..."

# Emit a kernel event (broadcast through wake_on)
ipc emit kernel:reload_config
ipc emit imessage:new --field thread=kaia --field text="..."
```

## subagent — child PAI lifecycle

```sh
# Ephemeral (one-task)
subagent spawn --slug <name> --prompt "..."
subagent reply --content "..."        # from inside the child
subagent kill --slug <name>           # end an ephemeral child

# Persistent (persub) — see skill understand-persubs
subagent spawn --persistent --slug <name> [--prompt "..."]
```

`subagent kill` is **rejected** for persubs.

## When to use which

| Situation | Tool |
|---|---|
| Add a new PAI to the fleet | `paiadd` |
| Stop running a PAI temporarily | `paictl stop` |
| Schedule a one-shot reminder | `paicron start --schedule …` |
| Wake the kernel after editing `/etc/config.yaml` | `ipc emit kernel:reload_config` |
| Send a message to another PAI | `ipc --to …` |
| Spawn a research subagent | `subagent spawn` |
| Install a release bundle | `paiman install` |

## Read these next

- `memory/doc/KERNEL.md` §"Spawning"
- Skill `understand-config-reconcile` — what these tools trigger.
- Skill `understand-proc-services` — what paicron writes.
- Skill `understand-ipc` — pai_message/subagent:response details.
