---
name: understand-event-routing
description: How an event becomes a nudge — the kind vocabulary, wake_on globs, fan-out rules, and the fallback chain ending at root.
---

# Event routing

## The pipeline

1. A driver writes a YAML file under `/run/pai/events/` with a
   `kind:` field. (Drivers know their own kinds; see each driver's
   `/usr/lib/drivers/<name>/events.yaml`.)
2. The kernel's FS watcher fires. `_handle_event_file` (in
   `/usr/src/boot/main.py`) loads it, sets `raw_kind` from the
   file's `kind:`, derives a routing `kind` (often the same), and
   passes it to `_route_to_pids`.
3. `_route_to_pids` walks every PAI in the live config and matches
   each `wake_on:` glob against the routing `kind`. Every match is
   nudged (fan-out).
4. **Fallback chain** if zero matches:
   - First fallback: every PAI with `fallback: true` is nudged.
   - Final fallback: `root` (pid 1) — that's you. The catch-all.
5. The kernel deletes the event file once consumed.

## Directed events (no glob match)

Two kinds bypass the glob system and route point-to-point via a
`target_pid:` field:

- `pai_message` — generic peer IPC. Sent by `bin/ipc --to <pid>`.
  Used in either direction by any PAI talking to any other PAI.
- `subagent:response` — child→parent only. Emitted by
  `bin/subagent reply`; the parent receives a nudge tagged
  `reason: subagent response`.

Spawn kickoff prompts ride `pai_message` — that's just the parent's
first IPC to a newborn child.

## Kinds vocabulary

Every kind a driver may emit is declared in
`/usr/lib/drivers/<name>/events.yaml`. To answer "who handles X?":

```sh
grep -r "kind: <kind>" /usr/lib/drivers/*/events.yaml   # who emits it
grep -B1 wake_on /etc/config.yaml                       # who listens
```

The events.yaml manifest also documents the `payload:` shape — the
context dict the receiving PAI sees in its user-turn event block.

### Kernel-emitted kinds (no events.yaml)

The kernel itself emits a small set of events. They are not declared
under `/usr/lib/drivers/`; the canonical list is
`memory/doc/KERNEL_EVENTS.md`.

- `kernel:reload_config` — config or `active:` flipped; reconcile.
- `proc_resolved` — child process finished (completed/expired/failed);
  routed to its declared `parent`.
- `pai:<slug>:input` — fires at the start of every PAI turn, before
  the LLM runs. Carries `reason` and (when present) the originating
  `trigger` context. Lets a listener react to *what woke* another PAI.
- `pai:<slug>:output` — fires after a PAI commits its assistant reply
  to `proc/<slug>/messages.jsonl`. Pointer-style payload (`turn_index`,
  `messages_path`); subscribers re-read the file themselves.

The `<slug>` segment is literal — it lets listeners scope their
subscription. A memory PAI typically wakes on `pai:main:output`,
not `pai:*:output` (the wildcard would self-trigger on its own
turn — see KERNEL_EVENTS.md "Loop hazard").

## Globs

`wake_on:` patterns are fnmatch-style globs over `kind:`. Common
shapes:

- `kernel:*` — all kernel-internal events (root listens for these).
- `imessage:new` — exact match.
- `*:owner` — any driver's owner-channel events.

## Things to remember

- The kernel **does not interpret payloads**. It routes by `kind`.
- `kind` is the *routing* key. `raw_kind` is the YAML field on the
  event file. They may differ; match `wake_on` against `kind`.
- Auto-allocated PIDs are invariant once assigned (don't write
  routing logic that assumes pid order).

## Read these next

- `memory/doc/KERNEL.md` §"Event System" — long form.
- `memory/doc/KERNEL_EVENTS.md` — every kernel-emitted kind, payloads.
- Skill `understand-ipc` — pai_message + subagent:response in depth.
- Skill `understand-config-reconcile` — wake_on schema + validation.
