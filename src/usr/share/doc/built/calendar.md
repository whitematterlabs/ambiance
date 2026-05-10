# calendar

Reads today's macOS Calendar events via EventKit. One-shot CLI bin.

**Call it:**
```
bin/calendar --today          # today's events (default)
bin/calendar --date 2026-05-11  # specific date
```

**State:** None — pure read. No spool, no files written. Reads from the owner's `~/Library/Calendars/` via EventKit.

**When to use:** Whenever a PAI needs to check what's on the owner's calendar — daily rundowns, scheduling questions, "do I have time at 3pm?"

**When not to use:** For creating or editing events (not yet built). For recurring-event expansion beyond what Calendar.app itself exposes.

**Gotchas:** Duplicate holiday calendars (Apple's "Holidays in United States" + a "US Holidays" feed) may produce duplicate all-day entries. Fix in Calendar.app, not in the bin. EventKit access must already be authorized (grant once in System Preferences > Privacy > Calendars).
