# PAI macOS menubar app (MVP)

A thin SwiftUI menubar shell over the PAI kernel. **No kernel changes.** It's a
second client of the same filesystem the TUI talks to:

| What | How |
|---|---|
| Discover PAIs | poll `~/.pai/proc/<slug>/spec.yaml` + `status` |
| Receive | `DispatchSource` on the day-file's parent dir + 0.5s stat poll |
| Send | append to day-file + atomic-rename a YAML into `~/.pai/run/pai/events/` |

Both TUI and this app can be open against the same PAI simultaneously — they
hold no locks and own no exclusive state.

## Status

**MVP, dev builds only.** No code signing, no notarization, no Sparkle, no
native notifications, no Location/Contacts entitlements re-homed. The
TUI is unchanged and remains the daily driver. See the "Graduating from TTY
to .app" section in the repo root `CLAUDE.md` for the framing.

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

- **GUI:** open `macos/PAI.xcodeproj` in Xcode, ⌘R.
- **CLI:** `cd macos && xcodebuild -project PAI.xcodeproj -scheme PAI -configuration Debug build`
  then launch the `.app` from `build/Build/Products/Debug/PAI.app`.

The target is configured as a menubar agent (`LSUIElement = YES`), unsigned,
no sandbox — it reads/writes `~/.pai/` directly as the owner.

A `P·N` glyph appears in the menubar (N = running PAI count) or `P·!` if
the kernel is offline.

## Run the kernel

The app does **not** start the kernel. Either launch it manually:

```sh
cd ~/.pai && usr/bin/python -m boot run
```

…or install the LaunchAgent so it starts at login and respawns on crash:

```sh
cp macos/launchd/com.pai.kernel.plist ~/Library/LaunchAgents/
/usr/bin/sed -i '' "s|YOUR_HOME|$HOME|g" ~/Library/LaunchAgents/com.pai.kernel.plist
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.pai.kernel.plist
launchctl enable gui/$UID/com.pai.kernel
launchctl kickstart -k gui/$UID/com.pai.kernel

# logs
tail -f ~/.pai/var/log/kernel.out.log ~/.pai/var/log/kernel.err.log
```

Uninstall:

```sh
launchctl bootout gui/$UID ~/Library/LaunchAgents/com.pai.kernel.plist
rm ~/Library/LaunchAgents/com.pai.kernel.plist
```

## Verify end-to-end

1. Kernel running (manual or LaunchAgent).
2. Build & run the app. Menubar shows `P·1` (or more).
3. Click menubar icon → at least the default PAI appears in the list.
4. Click a PAI → chat window opens. Today's transcript renders.
5. Type a message → ⏎ or Cmd-⏎. Watch:
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
