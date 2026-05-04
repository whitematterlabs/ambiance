---
name: restart-driver
description: Use when a kernel-owned driver (imessage-in, imessage-out, gmail-in, proc-watcher, tailer) crashed with a transient cause and needs to be bounced.
---

# Restart a crashed driver

## When to use

- A `proc failed` event arrived for a driver slug.
- `/proc/<slug>/status` reads `failed`.
- The traceback in `/proc/<slug>/log.md` indicates a **transient**
  cause: DB busy, fs race, network blip, file lock contention.

## When NOT to use

If the traceback indicates a **structural** failure — surface, don't
restart:

- `ImportError`, `ModuleNotFoundError` — code is broken.
- `KeyError` / `AttributeError` on schema fields — config or upstream
  contract drifted.
- Repeated identical crashes after a restart — looping, not transient.

## Procedure

1. `tail -n 50 /proc/<slug>/log.md` — confirm transient.
2. `paictl restart <slug>` (or `paictl start <slug>` if stopped).
3. Wait one tick; recheck `/proc/<slug>/status`.
4. Append one line to `/proc/root/log.md`:
   `restarted <slug> after <one-line cause>`.
5. If it crashes again within a minute — escalate. Do not loop.

## Escalation line

```
/var/spool/communication/messages/me/1/<today>.md
```

Append: `[<HH:MM>] root: <slug> looping on <cause>; needs your eyes`.
