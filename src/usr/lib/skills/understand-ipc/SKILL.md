---
name: understand-ipc
description: How PAIs talk to each other — pai_message and subagent:response, the bin/nudge and bin/subagent CLIs, ephemeral vs persistent subagents.
---

# Inter-PAI IPC

PAIs don't call each other synchronously. Every cross-PAI exchange
goes through the **event bus** as a directed event with a
`target_pid:` field — no `wake_on` glob fan-out, point-to-point.

## Two directed kinds

| Kind | Direction | Emitter | Use |
|---|---|---|---|
| `pai_message` | any → any | `bin/nudge --to <pid> --content "..."` | generic peer IPC |
| `subagent:response` | child → parent | `bin/subagent reply --content "..."` | child reporting back |

Spawn kickoff prompts ride `pai_message` — the parent's first IPC
to a newborn child is just a regular message.

The receiving PAI gets the event in its user turn. For
`subagent:response`, the parent additionally sees `reason: subagent
response` so it knows at a glance the message is from one of its
own children.

## CLIs

```sh
# Send a message to PAI at pid 2
bin/nudge --to 2 --content "fyi: gmail driver restarted"

# Address by slug also works for persubs
bin/nudge --to pai.memory --content "remember: the owner likes earl grey"

# Emit a kernel event (no target_pid; broadcast through wake_on)
bin/nudge emit kernel:reload_config

# Spawn an ephemeral subagent (one task, then done)
bin/subagent spawn --slug research-flights \
    --prompt "find me flights to istanbul"

# Spawn a persistent subagent (persub)
bin/subagent spawn --persistent --slug memory \
    --prompt "you curate knowledge"   # optional under --persistent

# Child reports back (uses $PAI_PARENT to know where to send)
bin/subagent reply --content "found: THY 1234 at $452"

# End an ephemeral subagent
bin/subagent kill --slug research-flights
```

`bin/subagent kill` is **rejected** for persubs — they live until
the parent shuts down. See skill `understand-persubs`.

## Subagent flavors

| | Ephemeral | Persub |
|---|---|---|
| Lifetime | one task | parent's lifetime |
| Slug | `<name>-YYYY-MM-DD` | `<parent>.<name>` |
| Kickoff | `--prompt` becomes `pai_message` | none — boots idle |
| Self-terminate | `bin/subagent kill` works | rejected |
| System prompt | `usr/share/prompts/subagent.md` | `subagent-persistent.md` |
| Spec marker | `persistent: true` only | `persistent: true` + `persub: true` |

A persub is reachable like any other process:
`bin/nudge --to <parent_slug>.<dep_name> --content "..."`.

## Why this matters

A parent can drive N concurrent children without blocking — every
turn is mediated by the bus, not by a synchronous call. The kernel
nudges the parent on each child reply.

## Read these next

- `memory/doc/KERNEL.md` §"Inter-PAI messaging"
- `memory/doc/PERSUBS.md` — full persub spec.
- `/usr/src/bin/subagent.py` — the subagent CLI.
- Skill `understand-persubs` — declarative `dependencies:` stanza.
- Skill `understand-event-routing` — directed vs broadcast events.
