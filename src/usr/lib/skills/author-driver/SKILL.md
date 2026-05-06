---
name: author-driver
description: Howto for creating a new driver — three-location split, events.yaml manifest, filesystem-based kernel discovery, deploy flow. Reference when scaffolding a new event source.
---

# Authoring a driver

A driver owns the on-disk shape of an external surface (messages,
email, calendar, contacts). The kernel routes its events but does
not interpret them.

## The three slots

| Slot | What you create | What you don't |
|---|---|---|
| `/usr/lib/drivers/<name>/` | code + `events.yaml` + `package.yaml` | runtime state |
| `/sys/drivers/<name>/` | (created at runtime by the driver) | code |
| `/proc/<slug>/` | (created at runtime by the kernel) | code or runtime state |

There is **no `/etc/drivers/`**. The kernel discovers drivers by
scanning `/usr/lib/drivers/*/events.yaml` at boot — no code
registration needed. Install the package, restart the kernel.

## Package layout

```
/usr/lib/drivers/<name>/
├── package.yaml        # name, kind: driver, version, description
├── events.yaml         # event vocabulary + process registry
├── __init__.py
├── inbound.py          # if the driver emits events (e.g. iMessage in)
└── outbound.py         # if the driver consumes events (e.g. iMessage out)
```

A driver may have either or both halves — `inbound`/`outbound` are
conventional, not required by name. The split is reflected in the
slug: process slugs are `<name>-in` / `<name>-out`; the package
name (under `/usr/lib/drivers/`) omits the suffix.

When authoring via coder, use `type: driver` in the brief — coder
will write to `/usr/lib/drivers/<name>/` directly.

## events.yaml manifest

This is the **routing vocabulary**. Every kind the driver may emit
must appear here. Cross-referenced by every PAI's `wake_on:` list.

```yaml
driver: imessage
description: Inbound and outbound iMessage routing.

events:
  - kind: imessage:new          # the routing key — what wake_on matches
    description: A new message arrived from a contact.
    emitted_by: src/drivers/imessage/inbound.py
    raw_kind: new_message       # the YAML `kind:` field on the event file
    payload:
      thread: string             # contact slug
      sender: string             # "me" if from_me, else contact slug
      text: string
      day_file: string           # relative path to the day's .md file

  - kind: imessage:owner
    description: Owner sent a message to PAI via the TUI.
    ...
```

`kind` is what `wake_on:` globs match. `raw_kind` is the YAML
`kind:` field on the event file dropped into `/run/pai/events/`.
Often the same; the distinction matters only when a driver emits
multiple routing kinds from one raw kind (or vice versa).

## Emitting an event

A driver writes `kind: <raw_kind>` plus payload fields to a YAML
file at `/run/pai/events/{timestamp}-{source}-{slug}.yaml`. The
kernel's FS watcher picks it up, reads, deletes, routes.

From driver code (Python), call the in-process helper:

```python
from boot import processes as P

P.emit_event({
    "kind": "imessage:new",
    "thread": "kaia",
    "sender": "kaia",
    "text": "dinner thursday?",
})
```

`bin/nudge` is the *peer-to-peer* CLI for one PAI to message another
(`bin/nudge --to <pid> --content "..."`). It is not the driver emit
path — drivers run as kernel-supervised processes and have direct
access to `P.emit_event`.

## Deploying the driver

The kernel discovers drivers by scanning `/usr/lib/drivers/*/events.yaml`
at boot. The full deploy flow once the code is written:

```sh
# 1. Install (if source isn't already at /usr/lib/drivers/<name>/)
sbin/paiman install /path/to/driver-source

# 2. Activate the process(es)
bin/paictl start <name>-in     # if inbound
bin/paictl start <name>-out    # if outbound

# 3. Restart the kernel so it discovers the new events.yaml
sbin/reboot
```

After restart, paictl's `active: true` spec is already on disk —
reconcile brings the driver up automatically. See skill
`kernel-restart` for restart procedure and caveats.

## Runtime state

Whatever cursors / last-event watermarks / queue depth the driver
needs go under `/sys/drivers/<name>/`. The driver owns this dir.
Read-mostly for everyone else — it's the sysfs-style introspection
window.

## Prefer native APIs over raw DB access

Before reading a SQLite file directly, check whether the OS or a
well-maintained library already exposes the same data through a
stable API. Native APIs handle schema migrations, permissions, and
change notifications for you.

| Surface | Prefer | Over |
|---|---|---|
| macOS Calendar | `EventKit` via PyObjC (`EventKit.EKEventStore`) | `~/Library/Calendars/*.sqlitedb` |
| macOS Contacts | `Contacts.CNContactStore` via PyObjC | `~/Library/Application Support/AddressBook/` |
| macOS Reminders | `EventKit.EKEventStore` (same store as Calendar) | raw SQLite |
| macOS Mail | `~/Library/Mail/` MBOX files | Mail.app SQLite indexes |
| iOS/macOS Health | `HealthKit` (requires entitlement — skip unless owner grants) | `~/Library/Health/` SQLite |

**Decision rule:** if a native framework exists for the platform,
use it. Fall back to SQLite/file parsing only when:
- No native API exists (iMessage → `chat.db` is the only interface), or
- The API requires entitlements the process can't get, or
- The API is significantly slower than direct DB access for the required polling frequency (rare — and if you're polling, rethink the design first).

## When you don't know the right approach

If you're unsure how to reach the external surface:

1. **Web search first.** Query for the app or data source + "macOS API",
   "PyObjC", "python", "reverse engineer". Look for prior art — someone
   has likely hit the same wall.

2. **Try the Accessibility API.** Any app that renders UI is readable via
   `ApplicationServices.AXUIElement`. Use it when there's no data API but
   you can observe state or trigger actions through the app's UI:

   ```python
   from ApplicationServices import AXUIElementCreateApplication, AXUIElementCopyAttributeValue
   import AppKit
   ```

   Common use cases: scraping displayed data, driving a GUI app
   programmatically, listening for focus/selection changes.

3. **Other Apple developer APIs worth knowing:**
   - `NSWorkspace` — app launch/quit events, frontmost app, file associations
   - `CoreData` / `NSPersistentContainer` — read iCloud-backed stores
   - `FSEvents` via `watchdog` — low-latency filesystem change notifications
   - `ScriptingBridge` — AppleScript-over-Python for scriptable apps
   - `CFNotificationCenter` / `NSDistributedNotificationCenter` — inter-app
     broadcast events (e.g. media player state)

4. **Check the app's own IPC.** Many apps expose XPC services, UNIX sockets,
   or named pipes under `~/Library/` or `/tmp/`. `lsof -U` and
   `ls /tmp/*.{sock,pipe}` often reveal them.


## Don't

- Don't put driver code under `/usr/src/boot/`. That's kernel.
- Don't put user-editable config under `/etc/drivers/`. There
  isn't one. Driver config is the manifest at
  `/usr/lib/drivers/<name>/events.yaml`.
- Don't have the kernel interpret your payload. It routes by
  `kind` only; the receiving PAI parses the rest.
- Don't read `~/Library/Calendars/*.sqlitedb` directly when
  `EventKit` is available — Apple changes that schema without notice.

## Read these next

- `/usr/lib/drivers/imessage/` — reference implementation.
- `/usr/src/boot/main.py` — `_discover_driver_specs`, `_handle_event_file`,
  `_route_to_pids`.
- Skill `kernel-restart` — how to restart the kernel after install.
- Skill `understand-event-routing` — how `kind` becomes a nudge.
- Skill `understand-filesystem` — the three-location driver split.
