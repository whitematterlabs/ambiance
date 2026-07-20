# cal — macOS Calendar event fetcher

Fetches the owner's macOS Calendar events via osascript and prints them as
human-readable text.

## Usage

```
bin/cal                  # today's events (default)
bin/cal --today
bin/cal --tomorrow
bin/cal --date 2026-05-15
bin/cal --range 2026-05-09 2026-05-16   # inclusive
bin/cal --calendars       # list all calendar names
bin/cal --all             # include slow subscription calendars
bin/cal --help
```

## State

Read-only. Calls osascript → Calendar.app. No local state or cache.

## When to use

- Daily calendar overview (e.g. cron at 12pm)
- Quick check of today/tomorrow events
- Date-range lookups

## Gotchas

- Two subscription calendars ("RC Calendar", "Holidays in United States")
  cause AppleScript linear scans to hang. They are skipped by default.
  Use `--all` to include them (may be slow).
- 30s timeout on osascript calls; exits with clear error if hit.
- Requires macOS TCC permission for Calendar access (AppleScript).
