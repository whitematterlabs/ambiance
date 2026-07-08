# Subagent bundles

A **subagent bundle** is a reusable specialist template at `/usr/lib/subagents/<name>/`. It ships a role prompt and provider/model defaults that any parent PAI can pull in with `subagent spawn --package <name>`. One bundle, many parents.

This is the subagent side of the same bundle system that fleet PAIs use at `/usr/lib/pais/<name>/`. Both kinds are scaffolded by `paiman`, listed by `paiman list`, inspected by `paiman show <name>`.

## Why bundles

A subagent can be spawned **bare** (just `--slug` + `--prompt`) or **bundled** (`--package <name>` pulls defaults from `/usr/lib/subagents/<name>/`). Reach for a bundle when:

- The same specialist will be spawned by more than one parent.
- The role prompt is non-trivial (more than a few lines).
- You want `paiman list` / `subagent list` to advertise the role to operators or skills.

Bare spawns are fine for one-off children that won't be reused.

## Bundle layout

```
/usr/lib/subagents/<name>/
├── package.yaml      # kind: subagent + defaults
└── prompt.md         # role prompt
```

`package.yaml`:

```yaml
kind: subagent
description: knowledge curator specialist for a parent PAI
prompt: usr/lib/subagents/memory/prompt.md
provider: deepseek
model: deepseek-v4-pro
# Optional: install supporting bundles before this subagent is activated.
# deps:
#   - drivers/ax
```

Provider/model travel together — a bundle that sets one but not the other will compose oddly with parent fallback. Set both, or neither.

## Lifecycle

### Author a bundle

```sh
paiman init memory --type subagent
$EDITOR /usr/lib/subagents/memory/prompt.md
$EDITOR /usr/lib/subagents/memory/package.yaml      # set description, provider, model
```

### Use it from a parent's turn

```sh
bin/subagent spawn --slug memory-sweep --package memory --prompt '...'
```

`--package` pulls defaults; `--model provider/tag` overrides; absent both, the spawning PAI's model (then the fleet default) is inherited. Subagents do **not** persist across reboots.

## Resolution chain

For `provider`, `model` (highest wins):

1. `--model` on the CLI.
2. Bundle's `package.yaml`.
3. Inherited: spawning PAI's spec → fleet default → `DEFAULT_MODEL = deepseek/deepseek-v4-pro`.

The bundle's `prompt`/`prompt_dir` become the child's role prompt; `--prompt` is the task itself.

## Inspect

```sh
paiman list                  # all bundles, grouped by type
paiman show memory           # print resolved package.yaml
subagent list                # installed subagent bundles + descriptions
ls /usr/lib/subagents/       # raw view
```

## What bundles do NOT do (today)

- No version pinning. Bundles live at `/usr/lib/subagents/<name>/` flat; `/opt/<pkg>/<ver>/` is for future versioned releases.
- No live update of running subagents when a bundle changes — the spec is captured at spawn time. Respawn the subagent to pick up bundle changes.

## See also

- `src/boot/config.py` — `resolve_subagent_package`.
- `src/bin/paiman.py` — `init` / `list` / `show`.
- `src/bin/subagent.py` — `spawn --package`.
- Skill: `manage-subagent-bundles` — operator workflow for authoring/installing.
