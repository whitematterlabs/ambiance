---
name: author-pai-bundle
description: Howto for creating a new PAI bundle — package.yaml, prompt, paiman init scaffolding, paiadd to instantiate. Reference when adding a new fleet member.
---

# Authoring a PAI bundle

A PAI bundle is the **template** a PAI is instantiated from. Two
locations:

- `/opt/<pkg>/<ver>/` — release bundles (from `paiman install`).
- `/usr/lib/pais/<name>/` — **dev source**, edited in place;
  `paiadd` stitches directly from here, bypassing `/opt/`.

Bundle content is **immutable post-install**. Edits go to instance
state at `/var/lib/instances/<pai>/`.

## Layout

```
/usr/lib/pais/<name>/
├── package.yaml      manifest
└── prompt.md         role prompt
```

That's the whole bundle in v1. Heavier pieces (drivers, skills) are
**system-shared**, not vendored — declared in `package.yaml`,
resolved by `paiman`, installed once at
`/usr/lib/drivers/<name>/` and `memory/skills/<name>/`.

## package.yaml

```yaml
name: scheduler-pai
version: 0.1.0
description: Schedules and triages calendar events.
default_instance: scheduler

required_drivers:
  - name: gcal
    version: ">=1.0"

required_skills:
  - reload-config

requested_capabilities:
  - read: /var/lib/memory/people
  - write: /var/lib/instances/scheduler

# optional baseline overrides; these become the prompt/provider/model
# the new instance gets at paiadd time
defaults:
  provider: deepseek
  model: deepseek-v4-pro
  wake_on:
    - gcal:*
```

## prompt.md

The role prompt for this PAI. Same shape as
`/usr/share/prompts/pai_default.md`. Keep it minimal — accumulated
guidance belongs in the instance's `memory/private/`, not the
prompt.

## Scaffolding flow

```sh
paiman init <name>            # creates /usr/lib/pais/<name>/ skeleton
$EDITOR /usr/lib/pais/<name>/package.yaml prompt.md

paiadd <bundle>               # useradd-style wizard:
                              #   - asks for instance name (default from manifest)
                              #   - assigns a pid
                              #   - writes /etc/config.yaml entry
                              #   - creates /var/lib/instances/<name>/
                              #   - emits kernel:reload_config

# Lifecycle (after instantiation):
paictl stop <name>            # mark inactive (active: false on spec)
paictl start <name>           # re-activate
paidel <name>                 # remove fleet entry; preserves instance state
paidel <name> --purge         # also wipe /var/lib/instances/<name>/
```

All three of `paiadd`/`paidel`/`paictl start|stop` end by emitting
`kernel:reload_config`. **Hand-edit `/etc/config.yaml` only to fix
an entry** — adds and removes go through these tools.

## Persubs

If your new PAI needs a long-lived specialist child (memory
curator, GUI delegate), declare it under `dependencies:` in the
config entry — not as a separate bundle. See skill
`understand-persubs`.

## Don't

- Don't vendor drivers or skills inside the bundle. Declare them.
- Don't bake instance-specific state into the bundle. The bundle is
  the template; the instance is the configured copy.
- Don't write a prompt that duplicates `memory/doc/` material.
  The PAI can read docs at runtime via skills.

## Read these next

- `memory/doc/FILESYSTEM_v3.md` §"Bundle anatomy"
- Skill `understand-bundles-and-instances` — the trinity.
- Skill `kernel-tools` — paiman/paiadd/paidel/paictl/paicron.
- Skill `understand-config-reconcile` — what the wizard writes.
