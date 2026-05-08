# AX_PLAN — macOS Accessibility as a PAI sensor + actuator

## Goal

Use the macOS Accessibility API (AXUIElement / AXObserver) to give PAI:

1. An ambient semantic feed of the user's entire desktop (what app/window/element is focused, what changed, what was announced).
2. A deterministic actuation channel for any compliant app (press button, set value, raise window, show menu) without screenshotting + pixel clicks.

Vision/OCR fallbacks for opaque surfaces (Figma canvas, games, video) are explicitly **out of scope** for this driver. If we need them later, they belong in a separate `screen` driver behind its own TCC grant. ax-pilot can depend on it then.

This is macOS-only by design. Accessibility, Input Monitoring, and per-app Automation TCC grants are assumed handled.

## Architecture

Two layers, separated by the rule "drivers route, PAIs decide":

```
                    ┌──────────────────────────────────────┐
                    │  other PAIs (proactive, contextual)  │
                    │  consume events directly             │
                    └──────────────────┬───────────────────┘
                                       │
                                  event bus
                                       │
┌──────────────────────┐     ┌────────┴────────┐     ┌──────────────────────────┐
│  ax-pilot + siblings │◀───▶│  drivers/ax/    │◀───▶│  Swift sidecar           │
│  (persubs, long-     │ RPC │  (event router, │     │  (AXObserver fan-in,     │
│   lived, per domain) │     │  RPC dispatch)  │     │  AX queries)             │
└──────────────────────┘     └─────────────────┘     └──────────────────────────┘
```

- **`drivers/ax/`** (in pairegistry) — mechanical. Owns the on-disk shape of the desktop event feed. Exposes RPC primitives. No LLM. **One driver instance, one sidecar, many persub consumers.**
- **`pais/ax-pilot/` and siblings** (in pairegistry) — judgment. Long-lived persubs (one per domain), each subscribes to the driver with its own filter and answers requests from other PAIs.
- **Swift sidecar** — long-running child of the driver. Talks AX, emits NDJSON events, accepts NDJSON RPC. Concurrent request handling (see §Concurrency).

## Notification reception (called out)

The driver is, by construction, a system-wide notification sink. Three independent channels feed into a unified `ax:*` event stream:

1. **`ax:announcement`** — apps that explicitly fire `AXAnnouncementRequested` (Apple apps, a11y-conscious AppKit apps). High precision, low recall.
2. **`ax:live_region`** — web/Electron equivalent: `AXValueChanged` on elements with `AXARIALive` set. Catches Gmail/Linear/Slack in-app toasts.
3. **`ax:notification`** — observer attached to the Notification Center process (`com.apple.notificationcenterui`). Every macOS banner from every app, regardless of origin. Catch-all.

Channel 3 works because Notification Center is itself an ordinary AX-visible app: find its PID, `AXUIElementCreateApplication`, attach an `AXObserver`, listen for `AXWindowCreated` / `AXCreated`, read title + body via standard attribute fetches. No private API.

Combining all three, PAI sees:
- Every system-level banner (channel 3).
- Plus app-internal toasts that never reach Notification Center (channels 1, 2).

This makes "PAI as ambient notification receiver" a Phase 1 deliverable, not a separate driver.

## Driver: `drivers/ax/`

Lives at `~/Projects/pairegistry/drivers/ax/`. Installed via `paiman install ax`.

### Layout

```
ax/
  events.yaml              # manifest: emitted event types, RPC surface
  driver.py                # Python driver entry point (kernel-side)
  sidecar/
    Package.swift
    Sources/AXSidecar/...  # Swift sources, depends on AXSwift
    .build/release/axd     # built binary, shipped or built on install
  README.md
```

### Lifecycle

- Driver starts → spawns `axd` sidecar as subprocess.
- Sidecar watches `NSWorkspace.runningApplications`, attaches `AXObserver` per PID, attaches to new launches.
- Sidecar emits NDJSON on stdout (events) and reads NDJSON on stdin (RPC).
- Driver translates sidecar events → kernel events; translates inbound RPC → sidecar commands.
- On revoked TCC grant: sidecar emits `ax:permission_lost`, driver surfaces it, ax-pilot bails gracefully.

### Outbound events (driver → kernel bus)

Coalesced + filtered in the sidecar before emission. Goal: 10–50 events/sec sustained, never more.

| Event | Trigger | Payload |
|---|---|---|
| `ax:focus_changed` | `AXFocusedUIElementChanged` | pid, app bundle id, window title, element role + identifier + visible text |
| `ax:window_changed` | `AXFocusedWindowChanged`, `AXMainWindowChanged` | pid, window id, title, frame |
| `ax:window_created` / `ax:window_destroyed` | window lifecycle | pid, window id, title |
| `ax:announcement` | `AXAnnouncementRequested` | pid, text, priority |
| `ax:live_region` | `AXValueChanged` on element with `AXARIALive` | pid, role, text, politeness |
| `ax:notification` | `AXWindowCreated` / `AXCreated` on `com.apple.notificationcenterui` | source app, title, body, timestamp |
| `ax:value_changed` | `AXValueChanged` (filtered) | pid, element ref, role, new value |
| `ax:selection_changed` | `AXSelectedTextChanged` (debounced 250ms) | pid, selected text snippet, range |
| `ax:url_changed` | `AXURL` delta on `AXWebArea` | pid, old url, new url |
| `ax:menu_opened` / `ax:menu_item_selected` | menu lifecycle | pid, menu path |
| `ax:app_launched` / `ax:app_terminated` | NSWorkspace | pid, bundle id |
| `ax:permission_lost` | `kAXErrorAPIDisabled` from any call | which grant |
| `ax:secure_input_active` / `ax:secure_input_cleared` | `IsSecureEventInputEnabled()` transitions | active flag |

### Subscriptions (event filtering for multiple persubs)

One driver. Multiple long-lived **persub** consumers — each its own process with its own identity (e.g., `gmail-watcher`, `mail-pilot`, `linear-pilot`, `notif-pai`). The driver fans out events through subscriptions with filter predicates.

```
gmail_watcher subscribes: { bundle_id: "com.google.Chrome", url_prefix: "https://mail.google.com/" }
mail_pilot    subscribes: { bundle_id: "com.apple.mail" }
notif_pai     subscribes: { event_types: ["ax:notification", "ax:announcement"] }
```

Filter predicates can match on: `pid`, `bundle_id`, `window_title` (regex), `url_prefix` (for AXWebArea), `event_types` (allowlist), `role`. The driver evaluates filters per event and only delivers to matching subscribers.

RPC is unscoped — any persub can call any RPC method with any ref. Refs are valid until the underlying element is destroyed; stale-ref errors are a normal failure mode, not a security boundary. If ax-pilot for Mail decides to act in Banking.app, that's a persub-level policy concern, not something the driver polices.

Refs are tagged at issue-time with the subscription that produced them. Cross-subscription use is allowed but logged — useful for debugging "why did notif-pai's ref end up in mail-pilot" mysteries without enforcing a boundary that doesn't exist at the OS layer.

### Concurrency (one driver, many persubs)

One driver + one sidecar handles N persubs because:

- `AXUIElementRef` is `(pid, element)` at the OS layer; there's no per-client AX session. Two persubs querying Mail and Chrome simultaneously hit independent AX trees with no contention.
- `AXObserver` attachment is per-app, not per-client. The sidecar already needs one observer graph to produce the event feed — additional persubs are free on the observation side.
- N sidecars would race on `AXEnhancedUserInterface` toggles and multiply observer fan-in. Don't do it.

What the sidecar must get right:

- **Concurrent RPC dispatch.** Tagged request IDs over the socket; replies route back by ID. A 400ms `dump_tree(Mail)` must not head-of-line-block a `read_attr` on Chrome. Worker pool or async dispatch — not a serialized queue.
- **Event fan-out is the only scoped surface.** Subscriptions filter events before delivery. RPC stays unscoped.
- **App-scoped state is shared.** `AXEnhancedUserInterface=YES` set by one persub is visible to all. Document it; don't try to virtualize it.

### Inbound RPC (caller → driver → sidecar)

Synchronous request/reply, JSON over the same socket multiplexed with events.

| Method | Purpose |
|---|---|
| `dump_focused_window(compress=true)` | Return compressed tree of focused window. Default scope. |
| `dump_tree(pid, root_ref?, max_depth?, compress=true)` | Explicit dump. |
| `query(pid, predicate)` | Find elements by role / identifier / visible-text / aria attrs. Returns refs + minimal context. |
| `read_attr(ref, attr)` | One-off attribute fetch. |
| `press(ref)` | `AXPress`. |
| `set_value(ref, value, chase_keystroke=auto)` | `AXSetValue`; if target is a web input and `chase_keystroke=auto`, send a no-op key after to fire JS change events. |
| `show_menu(ref)` | `AXShowMenu`. |
| `raise_window(ref)` | `AXRaise` + `AXMain=true`. |
| `set_focus(ref)` | `AXFocused=true`. |

### Tree compression (driver-owned, not subagent-owned)

Raw AX trees blow context windows. The sidecar compresses before returning. Rules:

- Drop containers with no `AXTitle` / no actionable role and a single child — fold child up.
- Drop elements with empty `AXValue` + empty `AXTitle` + no actions, unless they have children.
- For each kept element emit: `{ref, role, subrole?, identifier?, title?, value?, actions?, bbox, children}`.
- For lists / tables, only include visible (rendered) rows. Mark virtualization with `{"virtualized": true, "row_count": N}`.
- For text-heavy elements (web articles, documents), truncate `AXValue` at ~500 chars with a continuation token.
- Round-trip stability: refs are opaque strings the sidecar can resolve back to AXUIElements via a per-session ref table.

### Element ref strategy

`AXUIElementRef` values are not stable across reloads or even some SPA navigations. The sidecar maintains:

- A **ref table** keyed by an integer the driver hands out. Refs expire only on `AXUIElementDestroyed` from the owning observer, or on app termination — no idle TTL. Persubs can sit on a ref across slow LLM turns without surprise expiry. Persubs can also explicitly `release(ref)` to free entries.
- A **stable identity** per element — `(pid, AXIdentifier or path-to-root, role)` — used to re-resolve a ref if it goes stale. RPC calls that fail with stale-ref retry once with re-resolution.
- Each issued ref is tagged with the subscription ID that received it; cross-subscription use is logged (not blocked).

## Persubs: `pais/ax-pilot/` and siblings

Lives at `~/Projects/pairegistry/pais/ax-pilot/` (and `pais/gmail-watcher/`, `pais/mail-pilot/`, etc. as they emerge). **Persubs — persistent, long-lived subagents, one per domain.** Not invocation-pattern.

Why persubs:
- No cold-start cost on every GUI task.
- Each persub holds domain-specific state (e.g., Gmail watcher's "last seen message id") in its own home, no plumbing through callers.
- Subscription filter is set once at persub startup; no per-task scoping ceremony.
- Identity = process, which is what the driver fans events out to.

Per-domain split is judgment-based. Likely starting set: one generalist `ax-pilot` for ad-hoc app driving, plus specialized persubs as concrete needs emerge (notification handler, mail handler, etc.).

### Inputs

- High-level intent string ("reply to Sarah's last email saying yes").
- Optional: target app hint, target window hint, deadline.
- Read access to the recent event log and `dump_focused_window` RPC.

### Loop

```
while not done and budget remaining:
    observe = dump_focused_window() or dump_tree(target_pid)
    plan = LLM(intent, observe, history)
    for action in plan.actions:
        result = driver.rpc(action)
        if result.stale_ref: retry once with identity re-resolution
        if result.empty_tree: bail — vision/OCR is out of scope for this driver
        if result.blocked (Secure Input, AT-resistant surface): bail with reason
    done = LLM_check(intent, observe_after)
```

### Fallback ladder

1. AX tree primitives (default).
2. AppleScript via `osascript` for the ~30 scriptable Apple apps where it's strictly better (Mail, Safari, Finder, Music, Reminders, Notes, Calendar, Messages, ...). Driver exposes `osa(script)` as a separate RPC; ax-pilot picks it when target app is in the scriptable set.
3. CGEvent synthesis (last resort) for stubborn web forms where `set_value` + chase-keystroke fails.

Vision/OCR is deliberately not on this ladder. If an AX tree is empty, ax-pilot bails and surfaces it; we'll revisit a separate `screen` driver if real workloads demand it.

### Non-goals (initially)

- No browser-tab management, no shell command execution — that's other drivers' jobs.
- No long-horizon multi-app workflows in v1; one app, one window, one task.

## Permissions / TCC

Three separate grants to handle:

| Grant | Used for | Failure mode |
|---|---|---|
| Accessibility | AXUIElement, AXObserver | `kAXErrorAPIDisabled` |
| Input Monitoring | CGEvent fallback (last-resort actuation) | events silently dropped |
| Automation (per-app) | osascript / AppleEvents | per-app TCC prompt |

Driver emits `ax:permission_lost` events with the specific grant. ax-pilot checks grants before strategies that need them and surfaces missing grants to the user instead of silently failing.

Secure Input (password fields, 1Password unlock, sudo prompts in Terminal) is detected via `IsSecureEventInputEnabled()` polled in the sidecar; transitions surface as `ax:secure_input_active` / `ax:secure_input_cleared`. ax-pilot must check this flag before any keystroke-synthesis path and bail with a user-visible reason rather than silently dropping events.

## Phasing

### Phase 1 — Sensor only (no actuation, no subagent)

- Build Swift sidecar with AXObserver fan-in for the event types in the table above.
- Driver in pairegistry emits events to the kernel bus.
- Validate with a dumb consumer that just logs everything for a few hours of real desktop use.
- Tune coalescing thresholds until event rate stays under 50/sec sustained on a busy desktop.

**Exit criterion:** event log captures every meaningful focus / value / announcement / url change across Safari, Chrome, VS Code, Slack, Mail, Linear desktop without dropping or flooding. Notification reception verified across all three channels: a system banner from a random app shows up as `ax:notification`; an in-app toast in Gmail shows up as `ax:live_region`; an Apple-app announcement (e.g., Mail "message sent") shows up as `ax:announcement`.

### Phase 2 — Actuation primitives

- Add RPC surface to sidecar: `dump_focused_window`, `query`, `press`, `set_value`, `raise_window`, `set_focus`.
- Implement compression rules.
- Implement ref table + stale-ref retry.
- Validate manually via a small CLI (`bin/ax`) that wraps the RPC.

**Exit criterion:** can drive Mail.app to compose + send an email, drive VS Code to open a file via cmd palette, drive Safari to click a Gmail message, all from the CLI.

### Phase 3 — ax-pilot persub

- Build `pais/ax-pilot/` as a long-lived persub.
- Prompt design: focused window dump + intent + recent event log.
- Implement the action-loop with stale-ref retry and AX/osa/CGEvent fallback ladder.
- Start with single-app single-window tasks.

**Exit criterion:** can complete a handful of canonical tasks end-to-end from intent — reply to a specific email, send a Slack message, file a Linear issue, search and play a song in Music.

### Phase 4 — proactive consumers

- Build a sample PAI that consumes the ambient event feed and surfaces context proactively (e.g., "I see you opened the Stripe dashboard, here are your recent invoices").
- Validates the sensor-half-standalone hypothesis.

## Sidecar build & distribution

The sidecar is the first non-Python artifact in pairegistry. Decisions:

- Source under `drivers/ax/sidecar/` (Swift Package Manager).
- **Prebuilt binary shipped in the registry** at `drivers/ax/sidecar/.build/release/axd`, codesigned + ad-hoc-notarized for local use. `paiman install ax` symlinks it into place; no Xcode required on the install machine.
- `paiman build ax` (or a `Makefile` target in the bundle) rebuilds from source for developers. Build is opt-in, not part of the install path.
- macOS version pinning: state minimum macOS version in `events.yaml`; refuse to start sidecar on older.

## Key risks

- **Electron lazy a11y trees.** Mitigation: set `AXEnhancedUserInterface=YES` per app on first observer attach — but **per-app opt-in, not blanket**. Some apps (historically VS Code) degrade noticeably under it. Maintain a small per-bundle-id allow/deny list in the driver, default-on with known regressions excluded.
- **Web form `set_value` not firing JS events.** Mitigation: chase-keystroke option, default auto. CGEvent synthesis as last resort.
- **Tree dumps too expensive on Notion/Gmail.** Mitigation: compression + scoping to focused window + on-demand only. If still too slow, add per-app dump strategies.
- **Ref staleness in SPAs.** Mitigation: stable-identity re-resolution. If a ref is stale and identity doesn't re-resolve, ax-pilot re-dumps the tree.
- **Event flood during scrolling/typing.** "10–50/sec sustained" is optimistic — `AXValueChanged` on a typing field alone can hit that. Mitigation: aggressive coalesce + debounce in sidecar, never in Python. Per-event-type rate caps with overflow counters surfaced as a synthetic `ax:event_rate_capped` event.
- **Permission revocation mid-session.** Mitigation: explicit `ax:permission_lost` event, ax-pilot bails with user-visible reason.
- **NotificationCenter AX shape changes between macOS versions.** Mitigation: version-pinned selectors in the sidecar, regression test against a fixture per macOS major. On unknown major, log + emit `ax:notification_unsupported` instead of guessing.
- **Secure Input mid-actuation.** Mitigation: poll `IsSecureEventInputEnabled()`; ax-pilot checks before any keystroke path.

## Open questions

- Event log persistence — probably `/var/log/ax/events.ndjson` with rotation. Other PAIs `tail -f` it. Confirm during Phase 1.
- Multi-display handling — defer.
- Spaces / Mission Control — defer.
- Screen/OCR driver — defer until a real workload demands it; keep this driver AX-pure.
