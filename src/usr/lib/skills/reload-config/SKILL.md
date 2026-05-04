---
name: reload-config
description: Use when a `kernel:reload_failed` event lands or you've edited `/etc/config.yaml` and need the kernel to re-reconcile.
---

# Reload fleet config

## When to use

- `kernel:reload_failed` event arrived (context contains a `traceback`).
- You edited `/etc/config.yaml` to fix an obvious typo / missing
  required field / duplicate name / pid collision.

## Procedure

1. Read the traceback in the event context. Identify the offending
   entry by `name:` or pid.
2. `cat /etc/config.yaml` — locate the entry.
3. **Schema authority**: see `CONFIG_MANAGED_FIELDS` in
   `/usr/src/boot/config.py`. Required: `name`, `description`. PIDs
   1 and 2 are reserved (root, pai). Auto-allocated PIDs become
   invariant.
4. Apply the **smallest possible** edit that fixes the validation
   error. Do not rewrite unrelated entries.
5. Emit a fresh reload event:
   ```
   ipc emit kernel:reload_config
   ```
6. Watch `/proc/root/log.md` for confirmation; if it errors again,
   the fix wasn't complete — surface to operator.

## When NOT to fix yourself

- Provider/model changes — operator decides which model.
- Adding or removing a PAI — those go through `/sbin/paiadd` and
  `/sbin/paidel`, not hand-edits to `/etc/config.yaml`. The wizard
  asks for required context the operator should answer.
- `wake_on` route changes that affect more than the broken entry.

In those cases: append one line to
`/var/spool/communication/messages/me/1/<today>.md` describing what's
broken and what decision is needed. Do not guess intent.

## Verification

After a successful reload, `/proc/<name>/spec.yaml` will have the
managed fields rewritten to match `/etc/config.yaml`. Non-managed
fields are preserved.
