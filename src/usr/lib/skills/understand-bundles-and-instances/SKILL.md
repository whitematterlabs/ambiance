---
name: understand-bundles-and-instances
description: The bundle / instance / process trinity — what each means, where each lives, what survives uninstall. Read before reasoning about PAI identity.
---

# Bundle vs instance vs process

Three distinct concepts. Don't conflate them.

| Concept | What it is | Where it lives |
|---|---|---|
| **Bundle** | Template: a manifest + prompt | `/opt/<pkg>/<ver>/` (release) or `/usr/lib/pais/<name>/` (dev source) |
| **Instance** | A configured PAI: name, identity, private memory | `/var/lib/instances/<pai>/` (sacred) + `/home/<pai>/` (stitched view) |
| **Process** | A running PAI | `/proc/<pai>/` |

## Bundle

```
/usr/lib/pais/<name>/         (dev path — paiadd stitches from here)
├── package.yaml              what this PAI declares it needs/provides
└── prompt.md                 role prompt
```

`package.yaml` declares:
- bundle name, version, description
- required drivers (with version constraints)
- required skills
- requested capabilities (informational)
- default instance name

**Drivers and skills are system-shared dependencies, not bundle-
vendored.** Two PAIs that need `gmail` share one installed copy at
`/usr/lib/drivers/gmail/`. Bundle content is **immutable**
post-install — edits go to instance state.

## Instance

```
/var/lib/instances/<pai>/
├── .meta.yaml         { bundle, version, source, added_at }
├── memory/private/    PAI's own writable memory
├── workspace/         persistent scratch
└── inbox/             events addressed to this PAI
```

**Sacred state.** Survives uninstall/reinstall. `paidel <name>`
removes the fleet entry and home stitching but **leaves
`/var/lib/instances/<name>/` intact**. Re-adding restores the PAI
with all memory/workspace.

`paidel <name> --purge` is the destructive variant.
`paiman uninstall <bundle>` refuses if any instance references it.

## Home (stitched)

`/home/<pai>/` is mostly **symlinks** pointing into:
- `/var/lib/instances/<pai>/` for private state
- `/var/lib/memory/` for shared state
- `/run/pai/events/` for the event inbox
- `/usr/bin/` for tools
- `/proc/` for process visibility

PID 1 (root) lives at `/root/` instead. Same stitching pattern.

A PAI's identity (name, owner, role) is **not** a text file in the
home — it's already in `/etc/config.yaml` and `/proc/<pid>/spec.yaml`.
Behavioral guidance accumulates in `memory/private/` like any other
learned context, not as a monolithic `directives.md`. Per-instance
prompt overrides happen by pointing `config.yaml`'s `prompt:` at a
different file under `/usr/share/prompts/`, not by stashing prompts
in the instance.

## Reserved PIDs

- `1` → `root` — kernel-internal events, errored nudges, fallback.
- `2` → `pai` — owner-facing PAI, catch-all.

Auto-allocated PIDs are invariant once assigned.

## Four tools, one layer each

| Tool | Layer | Purpose |
|---|---|---|
| `paiman` | bundles | install/uninstall/upgrade bundles |
| `paiadd` / `paidel` | instances | configure / unconfigure a PAI |
| `paictl` | runtime | start/stop fleet members (`active:` flag) |
| `paicron` | services | spawn cron jobs, watchers, async work |

See skill `kernel-tools` for the cheat sheet.

## Read these next

- `memory/doc/FILESYSTEM_v3.md` §"Bundle anatomy" / "Instance anatomy"
- Skill `kernel-tools` — paiman/paiadd/paidel/paictl/paicron.
- Skill `author-pai-bundle` — howto for a new PAI.
