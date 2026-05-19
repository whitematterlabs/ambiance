# GUI migration TODO

Tracking what has to land before PAI graduates from TTY to a real `.app`. See `CLAUDE.md` § "Graduating from TTY to .app" for the *why*. This file is the *what*.

The target shape is locked: **single window + `NSStatusItem` (not `MenuBarExtra`) + `AppDelegate`-owned state, sidebar drives a single detail pane.** Don't drift into multi-window or `MenuBarExtra`; both have been ruled out.

## Phase 0 — current state

- [x] Menubar app exists as a read-only filesystem client (`macos/PAI/`).
- [x] Reads `/proc`, registry, kernel log.
- [x] `AppDelegate`-owned `AppState`, sidebar + tab strip + detail panes (chat, activity, procs).

## Phase 1 — kernel lifecycle off the TTY

The app is useless if the kernel only runs when a terminal is open.

- [x] Write `~/Library/LaunchAgents/com.pai.kernel.plist` template; ship it under `src/boot/` or `sbin/` and install via `paifs-init` (or a new `sbin/pai-install-agent`). → `sbin/pai-install-launchd {install,uninstall,status}`; plist template at `macos/launchd/com.pai.kernel.plist`.
- [ ] Kernel must log to a fixed path (`/var/log/kernel.log` inside `$PAI_ROOT` already works) so the app can tail it regardless of who started the kernel.
- [ ] `AppState` detects "kernel not running" (no `/proc/kernel/pid` or pid is dead) and offers a single button: **Start kernel** → `launchctl kickstart gui/$UID/com.pai.kernel`.
- [ ] `sbin/reboot` keeps working — the launchd job must tolerate the in-place re-exec without launchd treating it as a crash loop (`ThrottleInterval`, `KeepAlive` semantics).
- [ ] Decide: does quitting the app stop the kernel? Default **no** — kernel outlives the UI. Quit menu item just closes the window.

## Phase 2 — app → kernel input channel

Today the app only *reads*. To send a message or trigger an action from the GUI we need one writable surface, picked once and frozen.

- [ ] Pick the channel. Options:
  - File drop into a watched inbox (`/var/spool/<pai>/inbox/`) — matches existing driver shape, zero new transport.
  - Unix socket at `/run/kernel.sock` — lower latency, but new surface for the kernel to own.
- [ ] Recommendation: **file drop first** (uses the send_message contract that already exists), socket only if latency hurts.
- [ ] Swift side: one `KernelClient` actor; every "send" path in the app goes through it. No ad-hoc `Process` calls scattered across views.
- [ ] Round-trip test: type in chat window → message appears in target PAI's day file → reply renders in chat.

## Phase 3 — bundle identity & native affordances

This is the actual reason to graduate. Without these, the `.app` is just Terminal with a status icon.

- [ ] Real bundle ID (`com.pai.app` or similar); update `Info.plist`.
- [ ] Ad-hoc code sign locally; defer notarization until a non-owner user is on the horizon.
- [ ] `NSUsageDescription` keys for whatever PAI actually wants permission to do:
  - [ ] `NSUserNotificationsUsageDescription` — required day one.
  - [ ] `NSLocationWhenInUseUsageDescription` / `NSLocationAlwaysAndWhenInUseUsageDescription` — only if a driver consumes location.
  - [ ] `NSContactsUsageDescription`, `NSCalendarsUsageDescription` — only when the corresponding driver is wired.
- [ ] `UNUserNotificationCenter` wiring: kernel emits a notification event → driver writes to an outbox the app watches → app posts the notification *as PAI*. (Not the kernel posting directly; the kernel has no AppKit.)
- [ ] Launch-at-login via `SMAppService` (the modern replacement for login items / launchd user agents inside an app bundle). This is separate from the kernel's launchd plist — one is "start the daemon", the other is "open the window when I log in".
- [ ] Dock icon policy: app is `LSUIElement = true` (status item only, no dock) **or** dockless with explicit window — pick one. Recommendation: `LSUIElement = true`, window opens on demand from the status item.

## Phase 4 — Xcode project hygiene

- [ ] `project.pbxproj` currently has churn from MVP scaffolding; do a single cleanup pass so file refs match disk.
- [ ] Build settings: hardened runtime on, sandbox **off** for now (the app reaches all over `$PAI_ROOT`; sandboxing is a Phase 6 problem).
- [ ] One scheme, Debug + Release, no test target until there's something worth testing.

## Phase 5 — what we are explicitly NOT doing yet

Don't get nerd-sniped into these before Phases 1–3 land.

- [ ] **Embedded Python + kernel inside the bundle.** Only matters when non-technical users install PAI. Until then, the `.app` runs against an externally-installed `~/.pai`.
- [ ] **Sparkle / auto-update.** Manual `xcodebuild` + drag-to-Applications is fine for an owner-only app.
- [ ] **Notarization.** Defer until distribution is a real question.
- [ ] **App sandbox.** Sandboxing while the kernel layout is still moving is wasted plumbing.
- [ ] **Multi-window, tabs-as-windows, detached panes.** Locked: single window, sidebar-driven.

## Done = ?

Daily use of PAI happens through the app. Kernel starts at login without a terminal. Notifications come from *PAI*, not Terminal. Permission prompts (when a driver first asks) say "PAI" in the dialog. The TTY still works for dev but is no longer the daily driver.
