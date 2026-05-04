---
name: manage-subagent-bundles
description: Use when authoring, listing, or inspecting reusable persub specialist templates at /usr/lib/subagents/. Read before scaffolding a new specialist (memory, computer-use, etc.) or when an operator asks "what specialists are installed?". For wiring a bundle into a parent's config, use manage-dependencies instead.
---

# Manage subagent bundles

A **subagent bundle** is a reusable persub template — role prompt + provider/model defaults — that any parent PAI can pull in via `dependencies: [{name: ..., package: <bundle>}]`. Bundles live at `/usr/lib/subagents/<name>/`. They are scaffolded, listed, and inspected with `paiman`.

If you only need to **wire an existing bundle** into a parent, that's `manage-dependencies`, not this. This skill covers **authoring and curating** the bundles themselves.

## Inspect what's installed

```
paiman list                       # all bundles, grouped by type
paiman show <name>                # print resolved package.yaml
ls /usr/lib/subagents/            # raw view
```

`paiman list` groups output by `pais:` and `subagents:`. Each line shows `<name>  [provider/model]  <description>`.

## Author a new bundle

1. Pick a name. Rules: no `/`, `.`, or leading `-`. Singular and role-shaped (`memory`, `computer-use`, `triage`).
2. Scaffold:
   ```
   paiman init <name> --type subagent
   ```
   This creates `/usr/lib/subagents/<name>/{package.yaml,prompt.md}` with `kind: subagent`.
3. Edit `package.yaml`:
   - Set `description:` to a one-line catalog blurb (this is what shows up in `paiman list`, **not** the role prompt).
   - Set `provider:` and `model:` together. Either both, or neither — mixing parent fallback for one and bundle for the other gives weird pairings.
4. Edit `prompt.md` — the actual role prompt. The persub will receive this on every turn, prepended to the persistent-subagent system block. Operating principles, what to remember vs ignore, when to stay quiet.
5. Verify:
   ```
   paiman show <name>
   ```
   Should print your `package.yaml`.

## Use a bundle

Two ways, depending on lifetime:

- **Durable** (survives reboot): edit `/etc/config.yaml`, add a dep with `package: <name>`. See the `manage-dependencies` skill.
- **Ad-hoc** (this parent's lifetime only): from a parent's turn,
  ```
  bin/subagent spawn --persistent --slug <name> --package <name>
  ```
  `--model provider/tag` overrides the bundle's model.

## When NOT to author a bundle

- The specialist is one-shot. Use ephemeral `bin/subagent spawn --slug X --prompt "..."` (no `--persistent`).
- The specialist will only ever live under one parent and the prompt is short. Inline `dependencies:` is simpler.
- You haven't decided provider/model. Don't ship a half-bundle — surface the choice to the operator.

## Editing a live bundle

A persub's spec is captured **at spawn time**. Editing `package.yaml` or `prompt.md` does not retroactively update running persubs.

To pick up bundle changes:
1. Stop the parent (it takes its persubs with it).
2. Restart — reconcile re-spawns from the new bundle.

For surgical updates without taking the parent down: edit the persub's `/proc/<parent>.<name>/spec.yaml` directly, then nudge it. (Out of band — prefer the restart path unless you know what you're doing.)

## Removing a bundle

There is no `paiman uninstall` yet. To remove:

1. Confirm no `/etc/config.yaml` `dependencies:` entry references it (`grep "package: <name>" /etc/config.yaml`). Remove any that do.
2. `rm -rf /usr/lib/subagents/<name>/`.

Existing running persubs that came from the bundle keep running with their captured spec — the bundle is only re-read on spawn.

## Authority

- `paiman` source: `/usr/src/bin/paiman.py` — `BUNDLE_TYPES`, `cmd_init`, `cmd_list`, `cmd_show`.
- Bundle resolution: `resolve_subagent_package` in `/usr/src/boot/config.py`.
- Spawn-side resolution chain: `cmd_spawn` in `/usr/src/bin/subagent.py`.
- Full reference: `/usr/share/doc/SUBAGENT_BUNDLES.md`.
