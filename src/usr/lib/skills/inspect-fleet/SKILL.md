---
name: inspect-fleet
description: Use to survey current fleet state — what PAIs/drivers exist, which are running, which are failed. The reflex when an event is ambiguous or you need ground truth before acting.
---

# Inspect the fleet

## One-shot survey

```
ls /proc/                      # every running PAI + driver
for d in /proc/*/; do
  printf "%-20s %s\n" "$(basename $d)" "$(cat $d/status 2>/dev/null)"
done
```

## Per-entity drill-down

- **A PAI** (`root`, `pai`, …):
  - `/proc/<name>/spec.yaml` — what reconcile gave it.
  - `/proc/<name>/log.md` — its operational tail.
  - Stitched home: `/root/` (pid 1) or `/home/<name>/`.
  - Sacred state: `/var/lib/instances/<name>/`.

- **A driver** (`imessage-in`, `gmail-in`, …):
  - `/usr/lib/drivers/<short-name>/events.yaml` — what kinds it emits.
  - `/usr/lib/drivers/<short-name>/` — code.
  - `/sys/drivers/<short-name>/` — runtime cursors.
  - `/proc/<full-slug>/` — process state.

  Note the slug split: process slugs end in `-in`/`-out`; the driver
  package name omits that suffix. There is no `/etc/drivers/`.

## Source of truth checks

- `/etc/config.yaml` — fleet declaration. If `/proc/` and config
  disagree, reconcile is stale → emit `kernel:reload_config`.
- `/usr/lib/drivers/*/events.yaml` — routing vocabulary. Cross-reference
  with `wake_on:` patterns in config.

## Common questions

- *Who would handle event X?* — grep `wake_on:` in `/etc/config.yaml`
  for a glob over X's `kind`. Zero matches → fallback PAIs (every
  entry with `fallback: true`); still zero → root.
- *Which driver emitted this kind?* — `grep -r "kind: <kind>" /usr/lib/drivers/`.
- *Is this PAI's config stale?* — diff its `/proc/<name>/spec.yaml`
  managed fields against `/etc/config.yaml`. Mismatch → reload.
