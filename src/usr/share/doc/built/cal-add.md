# cal-add

Creates an event in Apple Calendar via AppleScript (`osascript`).

## How to call it

```
cal-add TITLE START END [--notes NOTES] [--calendar NAME]
```

Example:
```
cal-add "Team standup" "2026-05-17 09:30" "2026-05-17 10:00" --calendar Work
```

## Where its state lives

Writes directly to Apple Calendar via EventKit (osascript bridge). No on-disk
state of its own — events land in the owner's calendar immediately. First run
may prompt for Calendar automation permission.

## When to use it vs. not

- **Use** when PAI learns of a specific event with a known date/time and wants
  it on the owner's calendar.
- **Don't use** for fuzzy/vague schedule info ("sometime next week") — have the
  calendar-agent PAI ask for clarification first.
- **Don't use** for reading or listing events — the calendar driver handles
  that.

## Gotchas

- macOS BSD `getopt` has no long-option support; manual arg parsing handles
  `--notes`/`--calendar`.
- START/END are validated via `date -j`, rejects END ≤ START.
- Calendar name is case-sensitive and must match an existing calendar exactly.
  Without `--calendar`, uses the first available calendar (usually "Calendar").
