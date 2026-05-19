# PAI finished notifications

The macOS app posts a native notification each time a PAI finishes a turn. Today the app is a read-only filesystem client and never surfaces anything outside its own window; the owner has to look at the chat tab to know a PAI replied. This change closes that gap using surfaces that already exist: the kernel's `pai:<slug>:output` event (`src/boot/nudge.py:471`) and the driver pattern locked in by `macos/GUI_MIGRATION_TODO.md` Phase 3 ("driver writes to an outbox the app watches").

## Architecture

Two pieces:

1. **Kernel-side audit log**: in `src/boot/nudge.py`, immediately after the existing `pai:<slug>:output` emit (`nudge.py:471`), append one JSON line to `$PAI_ROOT/var/log/turns.jsonl`. The kernel already owns turn-completion semantically (it runs the nudge), so the audit log is in-character — it does not require the kernel to know what a "message" is, only that a turn completed.
2. **`NotifyWatcher` in the macOS app** — tails `var/log/turns.jsonl`, posts a `UNUserNotificationCenter` notification per new line.

Why not a driver: events route from the kernel to **PAIs** (via `wake_on`), not to drivers. Drivers emit events; they do not subscribe. A driver-as-listener would need to be wrapped as a PAI, which would invoke an LLM on every turn-end — wasteful for a feature that just appends a line.

`var/log/turns.jsonl` format: one JSON object per line, fields `{ts, slug, turn_index}`. Append-only. Rotation is out of scope; lines are small enough that growth is negligible.

## Kernel change

Single edit in `src/boot/nudge.py` adjacent to the existing emit at line 471:

```python
P.emit_event({
    "source": "pai",
    "kind": f"pai:{pai_slug}:output",
    ...
})

# Append-only turn audit log. Consumed by the macOS app's
# NotifyWatcher; also useful as a generic turn-end ledger.
turns_log = paths.var_log() / "turns.jsonl"
turns_log.parent.mkdir(parents=True, exist_ok=True)
with turns_log.open("a") as f:
    f.write(json.dumps({
        "ts": _now_iso(),
        "slug": pai_slug,
        "turn_index": len(new_history),
    }) + "\n")
```

`paths.var_log()` already exists; `_now_iso()` follows whatever ISO helper `nudge.py` already uses (verify during impl — fall back to inline `datetime.now().isoformat()` if not).

## App

Path: `macos/PAI/NotifyWatcher.swift` (new).

Behavior on app launch (after `AppDelegate` confirms the kernel is reachable):

1. Call `UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound])`. If denied, set a flag on `AppState` that the Activity tab can render as a single-line banner. No retries; the owner re-enables in System Settings.
2. Open `$PAI_ROOT/var/log/turns.jsonl` and seek to EOF. This is the critical step — without it, a notification storm fires on every app launch for backlog accumulated while the app was closed. Backlog is intentionally dropped.
3. Watch the file with `DispatchSource.makeFileSystemObjectSource(fileDescriptor:eventMask:.extend, ...)`, the same pattern `KernelLog.swift` uses for the kernel log. On `.extend`, read forward from the saved offset, parse one JSON object per newline, post one notification per object.
4. Each notification: `UNMutableNotificationContent` with `title = "PAI \(slug) finished"`, empty body, no sound, `trigger = nil` (deliver immediately). Identifier = `"pai-finished-\(slug)-\(turn_index)"` so the system deduplicates if the watcher races.

Lifecycle: the watcher is owned by `AppDelegate` (matches the "AppDelegate-owned state" rule from memory). It survives window close; it stops only on app quit.

## Info.plist

Add one key:

- `NSUserNotificationsUsageDescription` = `"PAI posts a notification when one of your PAIs finishes a turn."`

The Phase 3 checkbox in `GUI_MIGRATION_TODO.md` for this key gets ticked.

## Data flow

```
PAI finishes turn
  -> nudge.py emits pai:<slug>:output (unchanged)
  -> nudge.py appends one line to var/log/turns.jsonl (new)
  -> NotifyWatcher's DispatchSource fires on .extend
  -> UNUserNotificationCenter posts "PAI <slug> finished"
```

## Edge cases

- **App not running when PAIs reply.** Lines accumulate in `turns.jsonl`. On next app launch, watcher seeks to EOF; backlog is not replayed as notifications. The file remains a useful audit log.
- **Permission denied.** Watcher still runs (reads lines, calls `add(request:)`); the system silently drops them. The Activity tab shows a banner pointing to System Settings.
- **File doesn't exist yet on first app run.** Watcher polls for the file's appearance on a 5s timer until it shows up, then attaches. (No PAI has finished a turn since `paifs-init` of a fresh `$PAI_ROOT`.)
- **Kernel append fails mid-turn.** Wrapped in a try/except so a disk-full / permissions error cannot break PAI replies. Failure is logged to the kernel log; notifications stop until resolved.
- **Multiple turns in fast succession.** Each turn = one line = one notification. macOS coalesces visually in Notification Center; no extra logic needed.
- **App-internal source-of-truth drift.** None — `turns.jsonl` is the only source. The app never reads `messages.jsonl` for this feature.

## Out of scope

- Click-to-focus deep linking (notification opens chat tab for that PAI).
- Suppression while app window is focused.
- Filtering persubs vs top-level PAIs.
- Log rotation for `turns.jsonl`.
- Notification body content (preview text, duration, tokens).

Each is additive; none change the outbox contract.

## Testing

- Kernel: existing pytest suite must stay green. Add a focused test that nudges a fake PAI to completion and asserts one line appended to `turns.jsonl` with the expected `{ts, slug, turn_index}` fields.
- App: manual round-trip — start kernel, send a message to a PAI from the TUI, watch notification appear.
- Backlog suppression: write 50 lines to `turns.jsonl` while app is closed, launch app, confirm zero notifications fire.
