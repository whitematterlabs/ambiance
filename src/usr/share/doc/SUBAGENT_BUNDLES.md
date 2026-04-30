# Subagent bundles

A **subagent bundle** is a reusable specialist template at `/usr/lib/subagents/<name>/`. It ships a role prompt and provider/model defaults that any parent PAI can pull into a `dependencies:` entry by name. One bundle, many parents.

This is the persub side of the same bundle system that fleet PAIs use at `/usr/lib/pais/<name>/`. Both kinds are scaffolded by `paiman`, listed by `paiman list`, inspected by `paiman show <name>`.

## Why bundles vs inline deps

A `dependencies:` entry can be **inline** (every field handwritten on the parent) or **bundled** (`package: <name>` pulls defaults from `/usr/lib/subagents/<name>/`). Reach for a bundle when:

- The same specialist will live under more than one parent.
- The role prompt is non-trivial (more than a few lines).
- You want `paiman list` to advertise the role to operators or skills.

Inline is fine for one-off children that won't be reused.

## Bundle layout

```
/usr/lib/subagents/<name>/
├── package.yaml      # kind: subagent + defaults
└── prompt.md         # role prompt
```

`package.yaml`:

```yaml
kind: subagent
description: long-lived knowledge curator for the parent PAI
prompt: usr/lib/subagents/memory/prompt.md
provider: deepseek
model: deepseek-v4-pro
# requires:
#   drivers: []
#   skills: []
```

Provider/model travel together — a bundle that sets one but not the other will compose oddly with parent fallback. Set both, or neither.

## Lifecycle

### Author a bundle

```sh
paiman init memory --type subagent
$EDITOR /usr/lib/subagents/memory/prompt.md
$EDITOR /usr/lib/subagents/memory/package.yaml      # set description, provider, model
```

### Use it from config

```yaml
- name: pai
  pid: 2
  dependencies:
  - name: memory
    description: long-lived knowledge curator for the parent
    package: memory
```

`ipc emit kernel:reload_config` (or kernel restart) — `_reconcile_persubs` resolves the bundle and spawns `/proc/pai.memory/`.

### Use it ad-hoc from a parent's turn

```sh
bin/subagent spawn --persistent --slug memory --package memory
```

Same resolution: `--package` pulls defaults; `--model provider/tag` overrides; absent both, `DEFAULT_MODEL` is used. Ad-hoc persubs do **not** persist across reboots — promote them to `dependencies:` for that.

## Resolution chain

For `prompt`, `provider`, `model` (highest wins):

1. Inline override on the dep entry (or `--model` / `--prompt` on the CLI).
2. Bundle's `package.yaml`.
3. Parent PAI's value.
4. (Model only) `DEFAULT_MODEL = deepseek/deepseek-v4-pro`.

`description` is required at the call site (dep entry or `--prompt`) — bundles only contribute a catalog blurb.

## Inspect

```sh
paiman list                  # all bundles, grouped by type
paiman show memory           # print resolved package.yaml
ls /usr/lib/subagents/       # raw view
```

## What bundles do NOT do (today)

- No `requires:` resolution (drivers/skills wiring is deferred — same status as `pai` bundles).
- No version pinning. Bundles live at `/usr/lib/subagents/<name>/` flat; `/opt/<pkg>/<ver>/` is for future versioned releases.
- No live update of running persubs when a bundle changes — the persub's spec is captured at spawn time. Stop the persub (parent shutdown) and let reconcile respawn it to pick up bundle changes.

## See also

- `PERSUBS.md` — persub lifecycle (parent ownership, `done` semantics, addressing).
- `src/boot/config.py` — `resolve_subagent_package`, `_reconcile_persubs`.
- `src/bin/paiman.py` — `init` / `list` / `show`.
- `src/bin/subagent.py` — `spawn --persistent --package`.
- Skill: `manage-subagent-bundles` — operator workflow for authoring/installing.
- Skill: `manage-dependencies` — wiring a bundle into a parent's config.
