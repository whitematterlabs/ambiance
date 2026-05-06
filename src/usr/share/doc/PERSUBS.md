# Persistent Subagents (persubs)

A **persub** is a long-lived specialist child of a PAI. Unlike an ephemeral subagent (one task, then `done`), a persub boots once, lives for the parent's entire lifetime, and is addressable by a stable name (e.g. `pai.memory`).

Use a persub for a child whose value is its accumulated state or its always-on availability:
- `memory` — continuously curates knowledge across the parent's conversations.
- `computer-use` — handles GUI tasks without bloating the parent's context.

Use an **ephemeral** subagent (no `--persistent` flag) for one-shot delegation: research, drafting, code review.

## How persubs differ from ephemeral subagents

| | Ephemeral | Persub |
|---|---|---|
| Lifetime | one task | parent's lifetime |
| Slug | `<name>-YYYY-MM-DD` (date-suffixed) | `<parent>.<name>` (deterministic) |
| Kickoff | `--prompt` becomes a `pai_message` | none — boots idle |
| Self-terminate | `bin/subagent kill` (either side) | rejected — only parent shutdown removes it |
| System prompt | `usr/share/prompts/subagent.md` | `usr/share/prompts/subagent-persistent.md` |
| Spec marker | `persistent: true` only | `persistent: true` **and** `persub: true` |

## Bundled persubs (recommended)

A persub's prompt/provider/model can come from a **subagent bundle** at `/usr/lib/subagents/<name>/` — same shape as a `pai` bundle, but `kind: subagent`. Bundles are scaffolded with `paiman init <name> --type subagent` and listed via `paiman list`.

Reference one from a parent's dep entry with `package:`:

```yaml
- name: pai
  pid: 2
  dependencies:
  - name: memory
    description: long-lived knowledge curator for the parent
    package: memory          # pulls prompt/provider/model from /usr/lib/subagents/memory/
```

Resolution chain for `prompt`/`provider`/`model` (highest wins): inline dep override → bundle → parent. The bundle must exist at config-load time; missing bundles raise `ConfigError` and the parent will not boot.

## Declarative creation (inline)

Add a `dependencies:` list to the parent's entry in `/etc/config.yaml`. Each entry is a mapping; `name` and `description` are required, the rest inherit from the parent.

```yaml
pais:
- name: pai
  pid: 2
  description: owner-facing PAI
  prompt: src/prompts/pai_default.md
  provider: deepseek
  model: deepseek-v4-pro
  fallback: true
  dependencies:
  - name: memory
    description: long-lived knowledge curator for the parent
    # optional overrides:
    # prompt: src/prompts/memory.md
    # provider: anthropic
    # model: claude-opus-4-7
  - name: computer-use
    description: GUI-task delegate
    prompt: src/prompts/computer_use.md
```

Reload (kernel restart, or `kernel:reload_config`). On reconcile:
1. `dependencies:` is persisted onto `/proc/pai/spec.yaml` as a managed field.
2. For each dep, the kernel spawns `/proc/pai.<dep_name>/` if it doesn't exist already (idempotent).
3. The persub's home tree (`/home/pai.<dep>/`) is stitched.

`dependencies:` field reference per entry:
| Field | Required | Notes |
|---|---|---|
| `name` | yes | no `/`, `.`, or leading `-`; unique per parent |
| `description` | yes | one-line summary |
| `package` | no | name of a `/usr/lib/subagents/<name>/` bundle to pull defaults from |
| `prompt` | no | path relative to repo root; overrides `package` |
| `provider` | no | overrides `package`; falls back to parent |
| `model` | no | overrides `package`; falls back to parent |
| `wake_on` | no | list of event-kind globs; `${parent}` expands to the declaring PAI's slug. Overrides `package`; no parent inheritance. |

Bare-name shorthand (`dependencies: [memory]`) is rejected in v1; bundle resolution comes later.

## Subscribing to the parent's events

A persub can `wake_on:` arbitrary event kinds, just like a top-level PAI. Bundles use `${parent}` so the same bundle works for any declaring PAI — at reconcile, `${parent}` is replaced with the parent's slug. The shipped `memory` bundle subscribes to its parent's outbound chat events:

```yaml
# /usr/lib/subagents/memory/package.yaml
wake_on:
  - pai:${parent}:output     # fires after parent commits a reply
```

For `pai` declaring `memory`, this materializes as `wake_on: [pai:pai:output]` on `/proc/pai.memory/spec.yaml`. Memory then wakes on every parent turn and curates asynchronously without the parent having to address it.

Resolution: dep override → bundle. There is no parent inheritance for `wake_on` — children don't share routing with their parent. To opt out of a bundle's default, set `wake_on: []` in the dep entry.

## Ad-hoc creation (from a parent's turn)

Inside a PAI turn, the parent can spawn a persub directly:

```sh
bin/subagent spawn --persistent --slug memory \
    --model deepseek/deepseek-v4-pro \
    --prompt "you curate knowledge"   # optional
```

This produces `/proc/<parent_slug>.memory/` with the same shape as a config-declared persub. `--prompt` is optional under `--persistent` (no kickoff event is emitted regardless).

Ad-hoc persubs do **not** auto-respawn at next boot — they're not in any config. To make them durable, declare them under `dependencies:`.

## Addressing a persub

From the parent's turn, the persub is reachable like any other process:

```sh
bin/nudge --to pai.memory --content "remember: the owner likes earl grey"
```

The persub replies via `bin/subagent reply`, which emits a `subagent:response` event the parent recognizes.

## Lifecycle

- **Spawn**: at parent boot (declared) or mid-turn (ad-hoc with `--persistent`).
- **Run**: idle between messages; replies via `bin/subagent reply`. Cannot self-terminate.
- **Teardown**: only when the parent stops.

`bin/subagent kill` — whether called by the parent or the persub itself — is rejected with:

```
error: 'pai.memory' is a persistent subagent and cannot be resolved;
remove it from /etc/config.yaml `dependencies:` and reload
```

To remove a persub, edit the parent's `dependencies:` and reload. (Removal-on-reload is **not** implemented in v1 — the persub remains until parent shutdown. Manually clean its `/proc/<slug>/`, `/var/lib/instances/<slug>/`, and `/home/<slug>/` if needed.)

## Spec layout

A persub's `/proc/<parent>.<dep>/spec.yaml` looks like:

```yaml
kind: pai
pid: 6
slug: pai.memory
description: long-lived knowledge curator for the parent
provider: deepseek
model: deepseek-v4-pro
parent: 2
persistent: true   # blocks nudge auto-resolve
persub: true       # blocks `subagent kill`; selects persistent prompt
spawned: '2026-04-30T14:18:39'
```

Both flags matter: `persistent` is shared with all subagents (means "doesn't auto-resolve after one reply"); `persub` is the new marker that locks `done` and swaps the system prompt.

## Out of scope (today)

- Bundle/manifest resolution for `dependencies: [memory]` shorthand.
- Crash recovery / parent-driven restart of a dead persub.
- Per-parent sacred-state namespacing (e.g. `/var/lib/instances/<parent>/children/<dep>/`).
- Cross-parent visibility (other PAIs addressing another parent's persub by short name).

## See also

- `KERNEL.md` — process lifecycle and event routing.
- `FILESYSTEM_v3.md` — `/proc/`, `/var/lib/instances/`, `/home/` layout.
- `src/bin/subagent.py` — CLI reference.
- `src/boot/config.py` — `_validate_pai_entry` and `_reconcile_persubs`.
