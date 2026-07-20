# write_calendar

Creates an event in Apple Calendar via EventKit.

## How to call it

```
write_calendar TITLE START END [--notes NOTES] [--calendar NAME]
```

Example:
```
write_calendar "Team standup" "2026-05-17 09:30" "2026-05-17 10:00" --calendar Work
```

## Capability gate

Writing is gated by `capabilities.calendar_write` in `/etc/config.yaml`, read
live on every call. Unless it is `yes`, the command refuses and writes nothing.
The owner flips it in the console (Calendar toggle). Reading the calendar with
`cal` is never gated.

## Where its state lives

Writes directly to Apple Calendar via EventKit (same framework as the read-side
`cal` bin — no AppleScript). No on-disk state of its own — a created event lands
in the owner's calendar immediately. First run may prompt for Calendar access
(System Settings > Privacy & Security > Calendars).

## When to use it vs. not

- **Use** when PAI learns of a specific event with a known date/time and wants
  it on the owner's calendar (and `calendar_write` is granted).
- **Don't use** for fuzzy/vague schedule info ("sometime next week") — have the
  calendar-agent PAI ask for clarification first.
- **Don't use** for reading or listing events — the calendar driver + `cal`
  handle that.

## Gotchas

- START/END are `"YYYY-MM-DD HH:MM"` in the owner's local timezone; END ≤ START
  is rejected.
- Calendar name is case-sensitive and must match an existing writable calendar
  exactly. Without `--calendar`, uses the default calendar for new events.
- If the capability is off the command exits non-zero with a clear message —
  don't claim an event was created.
