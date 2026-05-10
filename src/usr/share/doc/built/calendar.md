# calendar

A Swift bridge to macOS Calendar.app via EventKit. Lists and creates events.

## How to call it

bin/calendar --list [--days N]
bin/calendar --add TITLE --when WHEN

## Where its state lives

Reads the same EventKit store Calendar.app uses.

## Gotchas

- Empty-title events appear as [HH:MM]  (Calendar Name).
- Swift binary; needs Calendar access granted on first run.
