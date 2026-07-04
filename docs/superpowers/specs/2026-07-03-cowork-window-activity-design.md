# Cowork Mode — Window + Clipboard + File Activity Tracking (v1 slice)

**Status:** Draft, pending user review
**Date:** 2026-07-03

## Context

Coworking mode is a planned upgrade where PAI observes the owner's computer
activity — active app/window, clipboard, downloads, browser activity — to feel
like a real ambient assistant rather than something only reachable via chat.
That full scope spans several independent subsystems, so this spec covers the
first slice: **window/app-switch tracking**, a **piggybacked clipboard copy-log**
that rides on it, and **file-activity watching** (what happens to files in the
owner's key folders). Browser activity is explicitly out of scope here and will
get its own spec later, added as an additional process to the same driver
package (see Architecture).

The clipboard copy-log is deliberately scoped to what stays *event-driven*: on
each app-switch we already receive, we sample the pasteboard's `changeCount` and,
if it moved, log the new contents. This catches the common "copy something, then
switch apps" case without a polling loop. Change-accurate clipboard tracking
(which macOS only exposes via polling `changeCount` on a timer — a violation of
PAI's tickless dogma) and *paste* tracking (not observable from the pasteboard at
all; a paste is a read and leaves no trace — would need a CGEvent keyboard tap and
a new Input Monitoring TCC grant) are both explicitly deferred (see Future work).

File-activity watching is deliberately scoped to *observing what happens to
files*, **not classifying the operation**. We don't try to tell `mv` from `cp`
from a download from an app-save — that inference is lossy and the owner doesn't
need it. We log the raw filesystem event (path + change kind + timestamp) via
FSEvents, which is push-based and so stays within the tickless dogma. The one
enrichment we keep, because it's cheap and high-value, is reading a new file's
"where from" xattr so a download's source URL lands in the log.

We watch the owner's **whole home tree**, not a hand-picked set of folders — the
goal is "what happened to my files," and files the owner cares about live all
over `~/`, not just `~/Downloads`. The cost of that breadth is `~/Library` and
similar machine-paced churn, which we suppress with a denylist (see Data flow)
rather than by narrowing the watch root. This is why the driver asks for **Full
Disk Access** rather than the three scoped per-folder grants: `~/` spans several
TCC-protected subtrees, and without FDA macOS redacts events for the ones we
haven't been granted, giving only partial coverage. FDA is a heavier, scarier
ask than a per-folder prompt; that tradeoff is accepted deliberately in exchange
for complete home coverage, and disclosed via the toggle copy and the
`<capabilities>` block.

This is a meaningful departure from precedent: the existing `ax` driver is
deliberately scoped as "piloting, not surveillance" — no ambient firehose, no
observation without an explicit per-session `attach`. Cowork mode is PAI's
first ambient/observational driver, so it gets its own capability flag and a
dedicated on/off control (the "Cowork Mode" toggle) rather than reusing `ax`'s
piloting-only posture.

## Goals

- Capture every macOS app/window focus change (app name + window title) as it
  happens.
- **Enrich** each window event with the underlying *artifact* so PAI can read
  the content itself, not the screen: the browser's real URL, or the file path
  open in an editor/viewer (Preview, VSCode, etc.). PAI already has filesystem
  and web access — the window says *what*, PAI reads the *what* from disk/URL.
- **Attention:** attach an idle-time reading (seconds since last human input, via
  IOKit `HIDIdleTime` — not keylogging) to each event, and let dwell duration be
  derived retroactively from the switch log (time-to-next-switch). Together these
  distinguish "staring at a PDF for 20 min" from "PDF open in the background."
- Emit each change as a kernel event so PAI's normal reasoning loop can react
  per-event (no separate "nudge" logic — this rides the same event routing as
  every other driver).
- Keep a flat, greppable log of the activity independent of whether PAI
  reacted, so PAI can answer "what was I doing at 2pm" on demand.
- On each app-switch, sample the pasteboard's `changeCount`; if it changed
  since the last sample, log the new clipboard contents (event-driven copy-log,
  no timer).
- Watch the owner's whole home tree (`~/`) via FSEvents and log every file
  create/remove/rename/modify as it happens — the raw event, not a guessed
  operation. Exclude machine-paced noise (`~/Library`, caches, VCS/build dirs,
  journal files) via a denylist so the residual stream stays human-paced. Enrich
  a newly-appeared file with its `kMDItemWhereFroms` source URL when present.
- Ship as processes inside a `cowork` driver package designed to grow
  additional trackers (downloads, browser, richer clipboard) later without
  restructuring.

## Non-goals (this slice)

- Change-accurate clipboard tracking (every copy, even without an app switch) —
  requires polling `changeCount` on a timer, which violates the tickless dogma.
  Deferred.
- Paste tracking (⌘V) — not observable from the pasteboard; needs a CGEvent
  keyboard tap + Input Monitoring TCC. Deferred, separate spec.
- Classifying file operations (mv vs cp vs download vs app-save) — deliberately
  not attempted; we log raw FSEvents, not inferred intent.
- Watching the *entire* filesystem — v1 watches `~/` (minus the denylist), not
  `/` or other volumes.
- Browser history/tabs — future spec.
- Any proactive "nudge" heuristics (idle detection, focus-fragmentation
  alerts, etc.) — PAI decides per-event whether to act; no driver-side trigger
  logic.
- A query API over the log — PAI reads the NDJSON file directly when asked.
- Log rotation/retention policy — out of scope until it's actually a problem.

## Architecture

New driver at `~/Projects/pairegistry/drivers/cowork/`, following the existing
inbound-driver pattern (same shape as `imessage`, `email`, etc.):

- `package.yaml` — paiman manifest.
- `events.yaml` — declares two processes: `window_activity` (event kinds
  `cowork:window_changed`, `cowork:clipboard_changed`) and `file_activity`
  (event kind `cowork:file_changed`). A future spec appends `browser` as another
  process entry in this same file — same driver, same capability flag,
  independently addable without a rewrite.
- `window_activity.py` — pure Python via `pyobjc`: subscribes to
  `NSWorkspace.didActivateApplicationNotification`; on each activation, reads
  the frontmost app name and window title via the Accessibility API (the same
  TCC permission the `ax` driver already requires — no new permission prompt
  for owners who've already granted it to `ax`). On the same activation callback,
  it also reads `NSPasteboard.generalPasteboard().changeCount`; if it differs
  from the last seen value, it reads the current string contents and logs a
  clipboard entry. No new TCC — pasteboard reads need no permission. This rides
  the window-focus event, so it stays a single event-driven process, not a
  second poller.
- On the same callback it also **enriches** the event: for a browser, it reads
  the frontmost tab's URL (via AX, falling back to AppleScript/Automation);
  for an editor/viewer, it pulls the open file path from the AX document
  attribute or the window title. And it reads the current idle-seconds from
  IOKit `HIDIdleTime`. Enrichment is best-effort per app — an app we don't have a
  reader for just logs app + title with no `url`/`file_path`. The browser-URL
  path may prompt a one-time **Automation/AppleScript TCC** grant (light,
  per-target-app — not Full Disk Access). No screen capture, no vision model:
  content understanding is PAI reading the artifact PAI was pointed at.
- `file_activity.py` — pure Python via `pyobjc`: opens a single `FSEventStream`
  rooted at `~/` with `kFSEventStreamCreateFlagFileEvents`, so each callback
  carries per-file paths and change flags (created / removed / renamed /
  modified). Noise is suppressed in two layers: (1) `FSEventStreamSetExclusionPaths`
  for the big prefix-matchable offenders (`~/Library` above all — capped at 8
  paths by the API); (2) an in-callback ignore-list for the scattered patterns
  prefixes can't catch (`node_modules`, `.git`, `Caches/`, `~/.cache`, `*.tmp`,
  `*-wal`, `*-journal`, `.DS_Store`). Only events surviving both layers are
  logged/emitted, which keeps the residual stream human-paced. It logs the raw
  event and emits `cowork:file_changed`; it does not classify the operation. For
  a path flagged created/renamed it best-effort reads the
  `com.apple.metadata:kMDItemWhereFroms` xattr and, if present, includes the
  source URL. Watching `~/` requires **Full Disk Access** (not the scoped
  per-folder grant) — see Capability gating.

No new compiled/Swift sidecar. This follows the lesson already burned into
project history: `PAI.app` was deleted because an Xcode-only build broke setup
on machines with only Command Line Tools, and `ax`'s sidecar now ships as a
prebuilt binary specifically to avoid that trap. Both a window-focus listener
and an FSEvents watcher are simple enough that pure Python avoids the problem
entirely rather than working around it.

## Data flow

On every window-focus change:

1. `window_activity.py` builds a payload: `{app_name, window_title, pid,
   timestamp, idle_seconds}`, plus best-effort `url` (browsers) or `file_path`
   (editors/viewers) from the enrichment step. Dwell isn't stored on the event —
   it's derivable from the gap to the next line, so the log stays append-only and
   nothing is rewritten.
2. Appends it as one line to `/sys/drivers/cowork/window_activity.ndjson`
   (runtime state per `FILESYSTEM_v3.md` convention — plain text, greppable,
   tailable). One file per tracker so each stays independently inspectable as
   more trackers are added to the driver.
3. Calls `P.emit_event(payload)` with kind `cowork:window_changed`. The kernel
   routes this to the owner's PAI process exactly like any other driver event
   — no debouncing, no coalescing. Every switch wakes PAI; PAI decides per-event
   whether it's worth reacting to or staying silent.
4. On the same callback, compares `NSPasteboard.changeCount` to the last seen
   value (held in memory; seeded on first callback, so a copy made before the
   first app-switch after boot isn't retroactively logged). If it moved, reads
   the current string, appends one line to
   `/sys/drivers/cowork/clipboard.ndjson`, and emits `cowork:clipboard_changed`
   with `{content, app, timestamp}` (`app` = the app now frontmost, a best-effort
   attribution of where the copy likely came from — not guaranteed). Non-string
   pasteboard contents (images, files) are logged as a typed placeholder with no
   raw bytes.

Independently, on every filesystem change under a watch root:

1. `file_activity.py`'s FSEvents callback fires with one or more `(path, flags)`
   pairs. (`~/Library` and the other exclusion-path prefixes never reach the
   callback — they're filtered by FSEvents itself.)
2. Each path is checked against the in-callback ignore-list; matches are dropped
   before any work. This is what keeps the whole-`~/` watch human-paced — the
   per-event wake model (below) is only safe because the denylist has already
   removed the machine-paced churn.
3. For each survivor, builds a payload `{path, change, timestamp}` where `change`
   is the raw FSEvents flag set (created/removed/renamed/modified), not an
   inferred operation. If the path was created/renamed and carries a
   `kMDItemWhereFroms` xattr, adds `source_url`.
4. Appends one line per surviving event to
   `/sys/drivers/cowork/file_activity.ndjson` and emits `cowork:file_changed`.
   Same routing as the others — **every surviving change wakes PAI**, no
   coalescing (consistent with the window/clipboard trackers); the denylist, not
   debouncing, is what bounds the volume. FSEvents may coalesce rapid bursts
   itself; we log whatever the callback delivers without re-expanding.

No cursor file (unlike `imessage`) — this isn't replaying a backlog from an
external store, it's a live push stream starting from kernel boot. A missed
notification (rare) is simply absent from the log; there's nothing to repair.
(FSEvents does support a since-event-ID replay, but v1 doesn't use it — a
missed file event is treated the same as a missed window event: gone, not
recovered.)

## Capability gating & privacy

- New capability flag `cowork` added to `CAPABILITY_SPECS` in
  `src/boot/config.py`, tri-state (`no`/`ask`/`yes`) in the owner's
  `config.yaml` `capabilities:` block. **Default `yes`** — unlike existing
  capabilities (which fail closed), Cowork Mode ships on by default and is
  controlled via an explicit toggle rather than an install-time prompt.
- One **"Cowork Mode" toggle** in the web console (`src/usr/libexec/web`)
  gates the entire `cowork` driver package — not per-tracker switches. Both
  `window_activity` and `file_activity` check this same flag, and future
  processes (browser) will too, so the owner controls coworking mode as one unit.
- Flipping the toggle writes `capabilities.cowork: yes|no` to `config.yaml`
  and the driver reconciles live (same `active:`-flag reconcile pattern
  `paictl start/stop` already uses) — no restart required.
- Unlike existing capabilities (which freeze *outbound* sends), this one gates
  whether the driver captures at all: when the flag is `no`, both processes
  either don't run or immediately no-op — no window titles, no clipboard
  contents, and no file events are ever touched while disabled.
- **New TCC surface — Full Disk Access:** watching all of `~/` spans several
  TCC-protected subtrees, so the driver needs **Full Disk Access**, not the
  scoped per-folder grant. This is a *separate*, heavier permission than the
  Accessibility one `ax`/window-tracking already have, and can't be triggered by
  a passive prompt — the owner must add PAI in System Settings › Privacy &
  Security › Full Disk Access. Because Cowork Mode defaults on, the web toggle
  copy must explain this and link/walk the owner there; until it's granted,
  file-activity coverage is partial (macOS redacts protected paths) and
  `file_activity.py` logs one warning. Denial doesn't crash the driver — it
  exits cleanly and the window/clipboard processes are unaffected.
- **Browser-URL enrichment** may trigger a light, per-app **Automation
  ("controlling <browser>")** TCC prompt the first time it reads a tab URL via
  AppleScript. This is much lighter than FDA and is not required for the core
  window/idle capture — if denied, events simply lack `url`. Idle-seconds
  (`HIDIdleTime`) and file-path-from-title need no permission at all.
- The same flag feeds the `<capabilities>` prompt block, so PAI's own
  self-description discloses that it's watching window activity (including the
  open URL/file and whether the owner is active), clipboard copies, *and file
  activity across `~/`* when enabled — enforcement and disclosure stay in sync,
  per existing convention.

## Log format

`/sys/drivers/cowork/window_activity.ndjson`, one JSON object per line:

```json
{"ts": "2026-07-03T14:22:01Z", "app": "Google Chrome", "window": "Lecture 5: Dynamic Programming - YouTube", "pid": 1234, "idle_seconds": 3, "url": "https://youtube.com/watch?v=..."}
{"ts": "2026-07-03T14:41:12Z", "app": "Preview", "window": "contract.pdf", "pid": 1290, "idle_seconds": 640, "file_path": "/Users/x/Downloads/contract.pdf"}
```

`url`/`file_path` present only when enrichment resolved them; `idle_seconds` is
seconds since last human input at capture time. Dwell is not stored — derive it
from the timestamp gap to the next line.

`/sys/drivers/cowork/clipboard.ndjson`, one JSON object per line:

```json
{"ts": "2026-07-03T14:22:05Z", "app": "Google Chrome", "content": "the copied text", "type": "string"}
{"ts": "2026-07-03T14:23:10Z", "app": "Finder", "content": null, "type": "file-url"}
```

`/sys/drivers/cowork/file_activity.ndjson`, one JSON object per line (`change`
is the raw FSEvents flag list, `source_url` present only when the xattr was):

```json
{"ts": "2026-07-03T14:25:00Z", "path": "/Users/x/Downloads/contract.pdf", "change": ["created"], "source_url": "https://example.com/contract.pdf"}
{"ts": "2026-07-03T14:26:12Z", "path": "/Users/x/Desktop/notes.txt", "change": ["renamed"]}
{"ts": "2026-07-03T14:27:40Z", "path": "/Users/x/Downloads/old.zip", "change": ["removed"]}
```

All three append-only, never rotated/truncated by the driver itself.

## Error handling

- If Accessibility permission isn't granted, `window_activity.py` logs a
  single warning to its own process log (`/proc/<slug>/log`) and exits
  cleanly rather than crashing/retrying — same TCC-missing behavior as `ax`.
- If Full Disk Access isn't granted, `file_activity.py` logs one warning noting
  coverage is partial and keeps running against whatever paths it can see (FDA
  can be granted later without a code change — the next event stream picks up the
  now-visible paths). It fails independently of `window_activity` — one process
  degrading or dying doesn't take the other down.
- Dropped NSWorkspace notifications or FSEvents callbacks (rare) simply don't
  appear in the log — no cursor/backlog to recover, since this is a live
  stream, not a replay.

## Testing

Manual verification only for v1:

1. Toggle Cowork Mode on in the web console.
2. Switch focus between a few apps/windows.
3. Confirm `window_activity.ndjson` gets new lines and the PAI process
   receives `cowork:window_changed` events (visible in kernel/process logs).
   Check enrichment: a browser line carries `url`, a Preview/VSCode line carries
   `file_path`, and `idle_seconds` climbs when you stop touching the machine.
4. Copy some text, then switch apps; confirm `clipboard.ndjson` gets a line and
   a `cowork:clipboard_changed` event fires.
5. Download a file, and mv/rm one somewhere in `~/` *outside* the old three
   folders (e.g. `~/some-project/`); confirm `file_activity.ndjson` gets lines
   (with `source_url` on the download) and `cowork:file_changed` events fire.
6. Confirm the denylist works: normal app usage does *not* flood the log with
   `~/Library` / cache / `-wal` churn, and the event rate stays human-paced.
7. Toggle Cowork Mode off; confirm no further lines are appended to any log
   and no further events are emitted.

No automated test suite for this slice — it's OS-level capture, not logic;
existing kernel event-routing tests (if any) already cover event delivery.

## Future work (explicitly deferred)

- Change-accurate clipboard tracking — catch every copy, not just those before
  an app-switch. Requires polling `changeCount` on a timer; revisit only if the
  event-driven copy-log proves too lossy in practice, and design against the
  tickless dogma explicitly (e.g. a bounded low-frequency sampler with a clear
  justification) rather than a naive busy-poll.
- Paste tracking (⌘V) — needs a global CGEvent keyboard tap + Input Monitoring
  TCC grant; paste destination is unreliable. Separate, higher-privacy spec.
- Richer file classification (mv vs cp vs download) — only if PAI turns out to
  need the operation, not just the fact of change; would use the inode + xattr
  heuristics we deliberately skipped in v1.
- Owner-tunable denylist via the web console (v1 ships a fixed default denylist;
  the watch root is always `~/`).
- **Screen capture + vision understanding (Tier C)** — periodic screenshots of
  the active window run through a vision model, for content that isn't a readable
  artifact (a design tool, a game, a native app with no file/URL). Its own spec
  and its own capability: it needs Screen Recording TCC, real storage/compute per
  frame, captures everything on screen (passwords, DMs), and screenshot-on-a-timer
  is polling — all reasons it must be a deliberate, separately-consented,
  off-by-default layer, not part of v1. Tiers A+B (artifact enrichment + idle)
  already cover the common "PDF / VSCode / lecture" cases without it.
- **Video-call notetaker** — local system-audio capture (mic + system output) →
  transcript → summary/action items, reusing PAI's existing STT dispatch. Now has
  its own spec: `2026-07-04-notetaker-driver-design.md` (own driver, own opt-in
  flag, never on-by-default; manual trigger; Core Audio process taps; local-
  default/cloud-opt-in STT).
- Proactive nudge heuristics built on top of the logged activity stream, once
  there's real data to design against.
