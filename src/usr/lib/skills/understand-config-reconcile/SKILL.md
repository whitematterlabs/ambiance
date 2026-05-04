---
name: understand-config-reconcile
description: How /etc/config.yaml becomes /proc/<pai>/spec.yaml — schema, managed vs preserved fields, when reconcile runs, and what triggers re-reconciliation.
---

# Config and reconcile

## Source of truth

`/etc/config.yaml` declares the entire long-running PAI fleet. It
is the **only** place fleet membership is decided. Everything in
`/proc/` is derived from it.

```yaml
pais:
- name: root
  pid: 1
  description: kernel-internal events + errored nudges
  prompt: src/prompts/root.md
  provider: deepseek
  model: deepseek-v4-pro
  wake_on: [kernel:*]
- name: pai
  pid: 2
  description: owner-facing PAI; catch-all
  prompt: src/prompts/pai_default.md
  provider: deepseek
  model: deepseek-v4-pro
  fallback: true
  dependencies:                 # persubs (see understand-persubs)
  - name: memory
    description: long-lived knowledge curator
```

## Schema authority

`CONFIG_MANAGED_FIELDS` in `/usr/src/boot/config.py` is the
authoritative list of fields reconcile **manages** (rewrites onto
`/proc/<pai>/spec.yaml`). Required: `name`, `description`. PIDs `1`
and `2` are reserved (root, pai); auto-allocated PIDs are invariant
once assigned.

Fields **not** in `CONFIG_MANAGED_FIELDS` on `/proc/<pai>/spec.yaml`
(e.g. `spawned`, `pid`) are **preserved** across reconcile.

## When reconcile runs

1. **Boot** — every time the kernel starts, before any PAI is
   spawned. See `/usr/src/boot/phases/reconcile.py`.
2. **`kernel:reload_config` event** — emitted by `paictl
   start/stop`, `paiadd`, `paidel`, or manually with
   `ipc emit kernel:reload_config`.

## What reconcile does

For every entry in `/etc/config.yaml`:
- If `/proc/<name>/` doesn't exist → create it, write `spec.yaml`,
  spawn the supervisor.
- If it exists → rewrite the managed fields on `spec.yaml`, leave
  the rest. Restart only if managed fields actually changed.
- If a `/proc/<name>/` exists but is missing from config → mark
  inactive (paictl stop) or remove on `paidel --purge`.

Also reconciles **persubs**: each `dependencies:` entry produces
`/proc/<parent>.<dep>/`. See skill `understand-persubs`.

## Hand-edit only to fix

Add and remove PAIs through the wizards (`paiadd`, `paidel`).
Hand-edit `/etc/config.yaml` only to **fix** a malformed entry
(typo, missing required field, duplicate name, pid collision).
After any hand-edit, emit `kernel:reload_config`.

The `reload-config` skill walks the fix-up procedure.

## Read these next

- `/usr/src/boot/config.py` — schema, validators, managed fields.
- `/usr/src/boot/phases/reconcile.py` — the reconcile pass.
- Skill `reload-config` — the action playbook for fixing config.
- Skill `understand-persubs` — `dependencies:` semantics.
