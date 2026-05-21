# PAI macOS menubar app (MVP)

A SwiftUI/AppKit menubar shell over the PAI kernel. **No kernel changes.** It's a
second client of the same filesystem the TUI talks to:

| What | How |
|---|---|
| Discover PAIs | poll `~/.pai/proc/<slug>/spec.yaml` + `status` (1s timer) |
| Procs view | poll all `~/.pai/proc/<slug>/{spec.yaml,status,busy}` (1s timer) |
| Activity log | tail `~/.pai/var/log/kernel/kernel.log` (0.5s size-poll) |
| Receive chat | `DispatchSource` on the day-file's parent dir + 0.5s stat poll |
| Send chat | append to day-file + atomic-rename a YAML into `~/.pai/run/pai/events/` |

Both TUI and this app can be open against the same PAI simultaneously — they
hold no locks and own no exclusive state.

## Shape

One persistent window. The menubar icon is an `NSStatusItem` button — single
left-click toggles the window, right-click quits. (SwiftUI's `MenuBarExtra`
can't do click-to-open without a popover, so the menubar is plain AppKit;
state and the window are owned by `AppDelegate`.)

The window is a `NavigationSplitView`:

- **Sidebar** — *Overview* group (Activity, Processes) + *PAIs* group, each PAI
  row carries a status dot and inline busy reason/elapsed-seconds.
- **Detail** — swaps between `ActivityWindow` (colored live tail of
  `kernel.log`), `ProcsWindow` (sortable table of every `/proc/<slug>/`), or
  `ChatWindow` (per-PAI transcript with markdown rendering + tinted bubbles).

Red close button hides; the next menubar click brings the window — and its
sidebar selection — back.

## Status

**Self-contained, ad-hoc signed.** `./build.sh` produces a single PAI.app that
embeds the Python runtime + all deps + the kernel and owns the kernel as a
child. Still deferred: Developer ID signing, notarization, Sparkle, and re-homed
Location/Contacts/Calendar entitlements (the day a permission-bearing feature
lands, flip `CODE_SIGN_IDENTITY`/`DEVELOPMENT_TEAM` and re-enable the hardened
runtime — see `bundle-runtime.sh`). The TUI is unchanged and remains the daily
driver. See the "Graduating from TTY to .app" section in the repo root
`CLAUDE.md` for the framing.

## Build

**Prerequisite:** full Xcode (not just Command Line Tools). Install from the
Mac App Store, then:

```sh
sudo xcode-select -s /Applications/Xcode.app/Contents/Developer
```

The project is generated from `macos/project.yml` via
[XcodeGen](https://github.com/yonaskolb/XcodeGen). `project.pbxproj` is
committed so a fresh clone with just Xcode can build — XcodeGen is only
needed if you change `project.yml`.

To regenerate after editing the spec:

```sh
brew install xcodegen        # one-time
cd macos && xcodegen generate
```

Build & run:

- **Consolidated app (recommended):** `cd macos && ./build.sh` builds the app
  *and* embeds a self-contained Python runtime (interpreter + all deps + the
  kernel) into `Contents/Resources/runtime/`, then ad-hoc re-signs the bundle.
  Result: `macos/build/PAI.app` — a single app that contains the kernel and
  owns it. `bundle-runtime.sh` does the embedding step alone against an existing
  build.
- **App only (dev, no embedded runtime):** open `macos/PAI.xcodeproj` in Xcode
  and ⌘R, or `xcodebuild ... -configuration Debug build`. With no bundled
  runtime, the app falls back to launching the kernel from the FHS
  (`~/.pai/sbin/init`) — fast iteration on Swift without re-embedding Python.

The target is a menubar agent (`LSUIElement = YES`), ad-hoc signed, no sandbox —
it reads/writes `~/.pai/` directly as the owner.

The menubar shows an SF Symbol bubble — hollow when all PAIs are idle, filled
when any PAI is busy, exclamation-bubble when the kernel is offline.

## The app owns the kernel

Hit **Start kernel** (menubar icon → kernel menu, or the offline empty state).
`KernelLauncher` runs the kernel as a **child the app owns** — not a detached
daemon. In a consolidated build it runs the *embedded* interpreter
(`Contents/Resources/runtime/python`); otherwise it falls back to
`~/.pai/sbin/init`. Either way it passes `PAI_ROOT` and `PYTHONPATH=<root>/usr/lib`
(so the on-disk `drivers` namespace package resolves) and tees stdout/stderr
into `kernel.log`:

```sh
tail -f ~/.pai/var/log/kernel/kernel.log
```

Quitting PAI SIGTERMs the kernel (`applicationWillTerminate` →
`terminateKernelSync`) so its PAIs shut down cleanly — the kernel does **not**
outlive the app. You can still run a kernel by hand for dev
(`cd ~/.pai && usr/bin/python -m boot run`); background autostart via launchd
was removed.

## Verify end-to-end

1. Kernel running (manual or via the app's Start kernel button).
2. Build & run the app. Menubar shows the bubble icon with the running count.
3. Click menubar icon → window opens to the first PAI (or Activity if none).
4. Sidebar shows Activity / Processes / each running PAI. Click any to switch.
5. Type a message in a PAI's chat → ⏎ or ⌘-⏎. Watch:
   - day-file grows: `tail -f ~/.pai/home/pai/communication/messages/me/<pid>/$(date +%F).md`
   - YAML event lands then disappears: `ls -lt ~/.pai/run/pai/events/ | head`
   - PAI's reply appears in the window within seconds.
6. Open the same PAI in the TUI at the same time → both windows update on
   each new message. (Proves no exclusive ownership.)
7. Quit & relaunch the app mid-turn → the reply still lands in the day-file.

## Known limitations

- **Day-file race**: TUI + app appending concurrently could interleave a byte.
  Acceptable for MVP (one owner, one hand) but real.
- **Schema drift**: any kernel change to event YAML shape, day-file format, or
  `/proc/<slug>/` fields breaks the Swift parser silently. The long-term fix
  is a shared `pai-channel` library both clients call. Not MVP.
- **`MiniYAML`** in `PAIRegistry.swift` only parses top-level flat key/value
  pairs. That's sufficient for `spec.yaml`'s `kind`/`pid`/`description` —
  not a general YAML parser.
- **No PAI lifecycle controls**. Start/stop is `paictl`'s job.
