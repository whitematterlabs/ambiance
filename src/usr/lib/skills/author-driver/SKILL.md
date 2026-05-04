---
name: author-driver
description: Howto for creating a new driver — three-location split, events.yaml manifest, registry in main.py, repo source layout. Reference when scaffolding a new event source.
---

# Authoring a driver

A driver owns the on-disk shape of an external surface (messages,
email, calendar, contacts). The kernel routes its events but does
not interpret them.

## The three slots

| Slot | What you create | What you don't |
|---|---|---|
| `/usr/lib/drivers/<name>/` | code + `events.yaml` (installed from `~/Projects/pairegistry/drivers/<name>/`) | runtime state |
| `/sys/drivers/<name>/` | (created at runtime by the driver) | code |
| `/proc/<slug>/` | (created at runtime by the kernel) | code or runtime state |

There is **no `/etc/drivers/`**. Drivers are a code-time registry
in the kernel — see `DRIVER_SPECS` in `/usr/src/boot/main.py`.

## Source layout

Drivers live in **`~/Projects/pairegistry/drivers/<name>/`**, NOT in the pai pyproject repo. The pyproject repo holds only the kernel; userspace packages (drivers, skills, libs, pais) all live in pairegistry.

```
~/Projects/pairegistry/drivers/<name>/
├── __init__.py
├── events.yaml         # event vocabulary + payload shapes
├── package.yaml        # paiman manifest (processes, deps)
├── inbound.py          # if the driver emits events (e.g. iMessage in)
└── outbound.py         # if the driver consumes events (e.g. iMessage out)
```

A driver may have either or both halves — `inbound`/`outbound` are
conventional, not required by name. The split is reflected in the
slug: process slugs are `<name>-in` / `<name>-out`; the package
name (under `/usr/lib/drivers/`) omits the suffix.

## events.yaml manifest

This is the **routing vocabulary**. Every kind the driver may emit
must appear here. Cross-referenced by every PAI's `wake_on:` list.

```yaml
driver: imessage
description: Inbound and outbound iMessage routing.

events:
  - kind: imessage:new          # the routing key — what wake_on matches
    description: A new message arrived from a contact.
    emitted_by: drivers/imessage/inbound.py
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

Use `bin/ipc emit` for the simple case from a shell:

```sh
bin/ipc emit imessage:new \
  --field thread=kaia \
  --field sender=kaia \
  --field text="dinner thursday?"
```

## Registering with the kernel

Add an entry to `DRIVER_SPECS` in `src/boot/main.py`. This is
where the kernel learns the driver exists, which proc slugs it
owns, and how to start/stop it. **`paictl start <slug>` flips
`/proc/<slug>/spec.yaml` `active:` and emits `kernel:reload_config`**
— reconcile is event-driven, never polled.

## Runtime state

Whatever cursors / last-event watermarks / queue depth the driver
needs go under `/sys/drivers/<name>/`. The driver owns this dir.
Read-mostly for everyone else — it's the sysfs-style introspection
window.

## Don't

- Don't put driver code under `/usr/src/boot/`. That's kernel.
- Don't put user-editable config under `/etc/drivers/`. There
  isn't one. Driver config is the manifest at
  `/usr/lib/drivers/<name>/events.yaml`.
- Don't have the kernel interpret your payload. It routes by
  `kind` only; the receiving PAI parses the rest.

## Read these next

- `/usr/lib/drivers/imessage/` — reference implementation.
- `/usr/src/boot/main.py` — `DRIVER_SPECS`, `_handle_event_file`,
  `_route_to_pids`.
- Skill `understand-event-routing` — how `kind` becomes a nudge.
- Skill `understand-filesystem` — the three-location driver split.
