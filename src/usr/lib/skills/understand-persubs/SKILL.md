---
name: understand-persubs
description: Persistent subagents (persubs) — long-lived specialist children declared via dependencies in /etc/config.yaml. Differ from ephemeral subagents in lifetime and addressing.
---

# Persubs

A **persub** is a long-lived specialist child of a PAI. Unlike an
ephemeral subagent (one task, then `done`), a persub boots once,
lives for the parent's entire lifetime, and is addressable by a
stable name (e.g. `pai.memory`).

Use a persub when the value is **accumulated state** or
**always-on availability**:
- `memory` — continuously curates knowledge across the parent's
  conversations.
- `computer-use` — handles GUI tasks without bloating the parent's
  context.

For one-shot delegation (research, drafting, code review) use an
**ephemeral** subagent. See `understand-ipc`.

## Declarative creation (recommended)

Add a `dependencies:` list to the parent's entry in
`/etc/config.yaml`:

```yaml
pais:
- name: pai
  pid: 2
  description: owner-facing PAI
  ...
  dependencies:
  - name: memory
    description: long-lived knowledge curator
    # optional overrides; otherwise inherit from parent:
    # prompt: src/prompts/memory.md
    # provider: anthropic
    # model: claude-opus-4-7
  - name: computer-use
    description: GUI-task delegate
    prompt: src/prompts/computer_use.md
```

After edit: emit `kernel:reload_config`. Reconcile:
1. Persists `dependencies:` onto `/proc/pai/spec.yaml`.
2. For each dep: spawns `/proc/pai.<dep_name>/` if missing
   (idempotent).
3. Stitches `/home/pai.<dep>/`.

Required per dep entry: `name`, `description`. `name` must have no
`/`, `.`, or leading `-`, and be unique per parent. Bare-name
shorthand (`dependencies: [memory]`) is rejected in v1.

## Ad-hoc creation

```sh
bin/subagent spawn --persistent --slug memory \
    --model deepseek/deepseek-v4-pro \
    --prompt "you curate knowledge"   # optional under --persistent
```

Ad-hoc persubs **do not auto-respawn at boot** — they're not in any
config. To make them durable, declare under `dependencies:`.

## Addressing

```sh
bin/ipc --to pai.memory --content "remember: arda likes earl grey"
```

The persub replies via `bin/subagent reply`, emitting a
`subagent:response` event the parent recognizes.

## Lifecycle

- **Spawn**: at parent boot (declared) or mid-turn (`--persistent`).
- **Run**: idle between messages; replies via `bin/subagent reply`.
  Cannot self-terminate.
- **Teardown**: only when the parent stops.

`bin/subagent done` on a persub is rejected:

```
error: 'pai.memory' is a persistent subagent and cannot be resolved;
remove it from /etc/config.yaml `dependencies:` and reload
```

To remove: edit parent's `dependencies:` and reload. **Removal-on-
reload is not implemented in v1** — the persub remains until parent
shutdown. Manually clean `/proc/<slug>/`, `/var/lib/instances/<slug>/`,
`/home/<slug>/` if needed.

## Spec marker

```yaml
kind: pai
pid: 6
slug: pai.memory
parent: 2
persistent: true   # blocks nudge auto-resolve
persub: true       # blocks `subagent done`; selects persistent prompt
```

Both flags matter. `persistent` is shared with ephemeral subagents
("doesn't auto-resolve"). `persub` is the new marker that locks
`done` and swaps the system prompt.

## Read these next

- `memory/doc/PERSUBS.md` — full spec.
- `/usr/src/boot/config.py` — `_validate_pai_entry`,
  `_reconcile_persubs`.
- `/usr/src/bin/subagent.py` — CLI.
- Skill `understand-ipc` — pai_message, subagent:response.
