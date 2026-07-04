# Cowork Mode — Window Activity Tracking (v1 slice)

**Status:** Draft, pending user review
**Date:** 2026-07-03

## Context

Coworking mode is a planned upgrade where PAI observes the owner's computer
activity — active app/window, clipboard, downloads, browser activity — to feel
like a real ambient assistant rather than something only reachable via chat.
That full scope spans several independent subsystems, so this spec covers only
the first slice: **window/app-switch tracking**. Clipboard, downloads, and
browser activity are explicitly out of scope here and will get their own specs
later, added as additional processes to the same driver package (see
Architecture).

This is a meaningful departure from precedent: the existing `ax` driver is
deliberately scoped as "piloting, not surveillance" — no ambient firehose, no
observation without an explicit per-session `attach`. Cowork mode is PAI's
first ambient/observational driver, so it gets its own capability flag and a
dedicated on/off control (the "Cowork Mode" toggle) rather than reusing `ax`'s
piloting-only posture.

## Goals

- Capture every macOS app/window focus change (app name + window title) as it
  happens.
- Emit each change as a kernel event so PAI's normal reasoning loop can react
  per-event (no separate "nudge" logic — this rides the same event routing as
  every other driver).
- Keep a flat, greppable log of the activity independent of whether PAI
  reacted, so PAI can answer "what was I doing at 2pm" on demand.
- Ship as one process inside a `cowork` driver package designed to grow
  additional trackers (clipboard, downloads, browser) later without
  restructuring.

## Non-goals (this slice)

- Clipboard capture, download tracking, browser history/tabs — future specs.
- Any proactive "nudge" heuristics (idle detection, focus-fragmentation
  alerts, etc.) — PAI decides per-event whether to act; no driver-side trigger
  logic.
- A query API over the log — PAI reads the NDJSON file directly when asked.
- Log rotation/retention policy — out of scope until it's actually a problem.

## Architecture

New driver at `~/Projects/pairegistry/drivers/cowork/`, following the existing
inbound-driver pattern (same shape as `imessage`, `email`, etc.):

- `package.yaml` — paiman manifest.
- `events.yaml` — declares process `window_activity` with event kind
  `cowork:window_changed`. Future specs append `clipboard`, `downloads`,
  `browser` as additional process entries in this same file — same driver,
  same capability flag, independently addable without a rewrite.
- `window_activity.py` — pure Python via `pyobjc`: subscribes to
  `NSWorkspace.didActivateApplicationNotification`; on each activation, reads
  the frontmost app name and window title via the Accessibility API (the same
  TCC permission the `ax` driver already requires — no new permission prompt
  for owners who've already granted it to `ax`).

No new compiled/Swift sidecar. This follows the lesson already burned into
project history: `PAI.app` was deleted because an Xcode-only build broke setup
on machines with only Command Line Tools, and `ax`'s sidecar now ships as a
prebuilt binary specifically to avoid that trap. A window-focus listener is
simple enough that pure Python avoids the problem entirely rather than working
around it.

## Data flow

On every window-focus change:

1. `window_activity.py` builds a payload: `{app_name, window_title, pid,
   timestamp}`.
2. Appends it as one line to `/sys/drivers/cowork/window_activity.ndjson`
   (runtime state per `FILESYSTEM_v3.md` convention — plain text, greppable,
   tailable). One file per tracker so each stays independently inspectable as
   more trackers are added to the driver.
3. Calls `P.emit_event(payload)` with kind `cowork:window_changed`. The kernel
   routes this to the owner's PAI process exactly like any other driver event
   — no debouncing, no coalescing. Every switch wakes PAI; PAI decides per-event
   whether it's worth reacting to or staying silent.

No cursor file (unlike `imessage`) — this isn't replaying a backlog from an
external store, it's a live push stream starting from kernel boot. A missed
notification (rare) is simply absent from the log; there's nothing to repair.

## Capability gating & privacy

- New capability flag `cowork` added to `CAPABILITY_SPECS` in
  `src/boot/config.py`, tri-state (`no`/`ask`/`yes`) in the owner's
  `config.yaml` `capabilities:` block. **Default `yes`** — unlike existing
  capabilities (which fail closed), Cowork Mode ships on by default and is
  controlled via an explicit toggle rather than an install-time prompt.
- One **"Cowork Mode" toggle** in the web console (`src/usr/libexec/web`)
  gates the entire `cowork` driver package — not per-tracker switches. When
  future processes (clipboard, downloads, browser) land, they check this same
  flag, so the owner controls coworking mode as one unit.
- Flipping the toggle writes `capabilities.cowork: yes|no` to `config.yaml`
  and the driver reconciles live (same `active:`-flag reconcile pattern
  `paictl start/stop` already uses) — no restart required.
- Unlike existing capabilities (which freeze *outbound* sends), this one gates
  whether `window_activity.py` captures at all: when the flag is `no`, the
  process either doesn't run or immediately no-ops — no window titles are ever
  touched while disabled.
- The same flag feeds the `<capabilities>` prompt block, so PAI's own
  self-description discloses that it's watching window activity when enabled
  — enforcement and disclosure stay in sync, per existing convention.

## Log format

`/sys/drivers/cowork/window_activity.ndjson`, one JSON object per line:

```json
{"ts": "2026-07-03T14:22:01Z", "app": "Google Chrome", "window": "Gmail: Re: contract terms", "pid": 1234}
```

Append-only, never rotated/truncated by the driver itself.

## Error handling

- If Accessibility permission isn't granted, `window_activity.py` logs a
  single warning to its own process log (`/proc/<slug>/log`) and exits
  cleanly rather than crashing/retrying — same TCC-missing behavior as `ax`.
- Dropped NSWorkspace notifications (rare) simply don't appear in the log —
  no cursor/backlog to recover, since this is a live stream, not a replay.

## Testing

Manual verification only for v1:

1. Toggle Cowork Mode on in the web console.
2. Switch focus between a few apps/windows.
3. Confirm `window_activity.ndjson` gets new lines and the PAI process
   receives `cowork:window_changed` events (visible in kernel/process logs).
4. Toggle Cowork Mode off; confirm no further lines are appended and no
   further events are emitted.

No automated test suite for this slice — it's OS-level capture, not logic;
existing kernel event-routing tests (if any) already cover event delivery.

## Future work (explicitly deferred)

- `clipboard` process — capture copy events, higher privacy sensitivity.
- `downloads` process — watch Downloads folder / browser download events.
- `browser` process — URLs/tab titles, requires browser-specific integration.
- Proactive nudge heuristics built on top of the logged activity stream, once
  there's real data to design against.
