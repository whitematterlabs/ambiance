---
name: manage-dependencies
description: Use when adding, removing, or inspecting a PAI's persistent subagents (persubs) — long-lived specialist children declared under `dependencies:` in /etc/config.yaml. Read before editing dependencies or when an operator asks for a memory/computer-use/etc. specialist.
---

# Manage persistent subagents

## What persubs are

A persub is a long-lived child of a PAI, declared under that PAI's
`dependencies:` in `/etc/config.yaml`. It boots once at the parent's
boot and lives for the parent's whole lifetime. Slug shape:
`<parent>.<dep-name>` (e.g. `pai.memory`).

Persubs are **not** ephemeral subagents. They cannot be resolved with
`bin/subagent done` — only the parent's shutdown removes them. If you
want a one-shot worker, use plain `bin/subagent spawn` (no
`--persistent`) instead.

## Inspect what already exists

```
ls /proc/ | grep '\.'                 # all persubs (slug has a dot)
cat /proc/<parent>.<dep>/spec.yaml    # spec — must show persub: true
cat /proc/<parent>.<dep>/status       # should be "running"
cat /proc/<parent>/spec.yaml | grep -A20 dependencies
```

## Add a persub

1. `paiman list` — see which subagent bundles are installed. A bundle
   ships a default prompt/provider/model so a dep entry only needs
   `name`, `description`, and `package: <bundle-name>`. If no bundle
   fits, scaffold one with `paiman init <name> --type subagent` and
   edit `/usr/lib/subagents/<name>/prompt.md`.
2. `cat /etc/config.yaml` — locate the parent entry.
3. Append a `dependencies:` list (or extend the existing one) under
   the parent. Required: `name`, `description`. Use `package:` to
   pull defaults from a bundle (recommended), or inline
   `prompt`/`provider`/`model` to override.

   Bundled (preferred):
   ```yaml
   - name: pai
     pid: 2
     description: owner-facing PAI
     ...
     dependencies:
     - name: memory
       description: long-lived knowledge curator for the parent
       package: memory
   ```

   Inline (no bundle):
   ```yaml
     dependencies:
     - name: scratch
       description: ad-hoc child for one project
       prompt: src/prompts/scratch.md
   ```

4. Validate: `name` must be unique under that parent and contain no
   `/`, `.`, or leading `-`. Bare-string shorthand
   (`dependencies: [memory]`) is **not** supported in v1 — entries
   must be mappings. If `package:` is set, the bundle must exist at
   `/usr/lib/subagents/<package>/` or the kernel refuses to boot.
5. Reload:
   ```
   ipc emit kernel:reload_config
   ```
6. Verify: `/proc/<parent>.<dep>/spec.yaml` exists with `persub: true`
   and `parent: <pid>`. `/proc/<parent>.<dep>/status` is `running`.

## Remove a persub

There is no live-removal in v1. The persub keeps running until the
parent shuts down. To remove:

1. Delete the entry from `dependencies:` in `/etc/config.yaml`.
2. `ipc emit kernel:reload_config` so the parent's spec is updated.
3. The persub keeps running this session. To force it down now:
   stop the parent (it will take its persubs with it) — or, for a
   surgical removal, manually clean `/proc/<parent>.<dep>/`,
   `/var/lib/instances/<parent>.<dep>/`, and
   `/home/<parent>.<dep>/`.

`bin/subagent done` against a persub is **rejected** by design. Don't
try to use it as a teardown tool.

## When NOT to add a persub

- The work is one-shot (research, drafting, code review). Use
  `bin/subagent spawn --slug X --prompt "..."` ephemeral.
- The specialist would have no accumulated state across calls and no
  reason to be warm. A persub costs a process slot and an LLM context
  for every parent boot — only worth it for state or always-on
  responsiveness.
- The operator hasn't decided which provider/model. Don't pick for
  them — surface to operator.

## Healing

On kernel restart, every running proc is resolved to `stopped`. The
next boot's reconcile heals each declared persub back to `running`
automatically. If you see a persub stuck at `stopped` after reload,
that's a bug — surface to operator with the slug and traceback.

## Authoring a new bundle

If `paiman list` doesn't have a bundle for the role you need, switch to
the `manage-subagent-bundles` skill — that one covers `paiman init
--type subagent`, editing `package.yaml`/`prompt.md`, and bundle
lifecycle. Come back here once the bundle exists to wire it in.

## Authority

- Schema: `_validate_pai_entry` and `DEP_FIELDS` in
  `/usr/src/boot/config.py`.
- Spawn logic: `_reconcile_persubs` in the same file.
- Full reference: `/usr/share/doc/PERSUBS.md`.
- Bundle reference: `/usr/share/doc/SUBAGENT_BUNDLES.md`.
