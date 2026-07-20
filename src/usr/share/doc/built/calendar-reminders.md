# calendar-reminders

**What it is**: Schedules paicron one-shot reminders for upcoming Apple Calendar events, so the owner gets notified before meetings.

**How to call it**:
```
calendar-reminders --days 2 --minutes-before 15
```
Options: `--days N` (lookahead window, default 2), `--minutes-before M` (lead time, default 15), `--dry-run` (print without scheduling).

**Where its state lives**: Uses `paicron ensure` to register reminders; deduplication is by deterministic slug (`reminder-YYYYMMDD-HHMM-<hash>`). Actual paicron state lives under `/Users/arda/.pai/var/lib/instances/` per the paicron subsystem. No separate state file — idempotent via slug matching.

**When to use it vs. not**: Use to wire calendar events into PAI's reminder system. Run it as a cron (e.g., every 30 min via paicron) so new events and changes are picked up. Do NOT use for non-Apple-Calendar reminder sources — this bin depends on the `calendar` bin (EventKit bridge).

**Gotchas**:
- All-day events are skipped (no meaningful firing time).
- Events whose lead-time has already passed are skipped.
- Events with empty titles appear as "(RC Calendar)" — the reminder fires but the message is sparse.
- The consuming PAI (pid 2) receives reminders via `send-message`.
- Requires the `calendar` bin (paiman package `calendar`) to be installed.
