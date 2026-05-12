# calendar driver

Watches Apple Calendar via EventKit (NSNotificationCenter subscription, not polling) and emits `calendar:new`, `calendar:changed`, `calendar:removed` for events within the upcoming horizon.

## How to use

The driver runs as a kernel-supervised process:
```sh
paictl start calendar-in    # activate after install
paictl stop calendar-in     # deactivate
paictl status calendar-in   # check state
```

PAIs subscribe via `wake_on: ["calendar:*"]` in `/Users/arda/.pai/etc/config.yaml`.

## State

- **Code**: `/Users/arda/.pai/usr/lib/drivers/calendar/` (symlink → `/Users/arda/.pai/opt/paiman/calendar/`)
- **Runtime state**: `/Users/arda/.pai/sys/drivers/calendar/state.json` (cached event snapshot for diffing)
- **Process**: `/Users/arda/.pai/proc/calendar-in/`

## When to use

Use when a PAI needs to react to calendar changes — reminders before events, scheduling awareness, "what's next" queries. The driver emits the raw event stream; a waking PAI owns reminder timing and user-facing presentation.

## Gotchas

- **First boot** emits every upcoming event as `calendar:new` — PAIs should treat the initial burst as a seed, not as alerts.
- **Calendar permission** — on first run, macOS prompts for Calendar access in System Settings → Privacy & Security → Calendars. If denied, the driver idles.
- **EventKit-backed**, not raw SQLite. Apple schema migrations don't affect it.
- **300s safety-net refresh** catches missed notifications across suspend/resume.
- **Deploy needs two reboots**: one for driver discovery, one for `wake_on` config.
