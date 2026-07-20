# paicron ensure: cancelled-slug respawn fix

**What it is**: Bug fix in `paicron ensure` — the ensure command is now idempotent across all statuses, including "cancelled". Previously it would crash with `ProcessExists` when re-ensuring a slug whose prior run had completed and been resolved to "cancelled".

**How to call it**: `paicron ensure --slug <name> --run '<cmd>'` — works identically, just no longer errors on the second run.

**Where its state lives**: Source at `/Users/arda/.pai/usr/src/bin/paicron.py`. The fix adds `shutil.rmtree` after the `P.resolve` call in `cmd_ensure`, so the stale proc directory doesn't block the fresh `P.spawn`.

**When to use it vs. not**: This is transparent — all existing callers of `paicron ensure` (calendar-reminders, boot hooks, any idempotent service registration) benefit automatically.

**Gotchas**: Respawn loses the previous run's log.md — the proc directory is fully removed before re-creation. For long-lived services this is acceptable; the old log reflected a past run.
