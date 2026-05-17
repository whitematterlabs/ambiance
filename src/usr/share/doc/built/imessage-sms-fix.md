# imessage SMS fix (macOS 15 Sequoia)

The imessage outbound driver's AppleScript for 1:1 sends used
`buddy "handle" of targetService` which broke on macOS 15 — the
`service` object model returns "AppleEvent handler failed."

## What changed

Replaced `_applescript_for_1to1` to use chat-id-based addressing:

```applescript
tell application "Messages"
  set targetChat to chat id "iMessage;-;+handle"
  send "text" to targetChat
end tell
```

Chat IDs on Sequoia encode the service type directly:
`iMessage;-;+number` or `SMS;-;+number`. The `_send` fallback loop
already tries iMessage first, then SMS — unchanged.

## Gotchas

- For a brand-new handle with no chat history, `chat id "SMS;-;+X"`
  may error. The driver surfaces this as `send_failed` — the PAI can
  retry or the owner can send the first message manually to bootstrap
  the chat.
- Group chat sending (`_applescript_for_group`) was already chat-id-based
  and was not changed.

## Update 2026-05-16: added RCS fallback

macOS 15 Messages.app uses three service types in chat IDs:
`iMessage`, `SMS`, and `RCS`. The original fix only tried iMessage
and SMS — contacts using RCS (e.g., Android users on carriers with
RCS support) would fail with -1728 ("Can't get chat id").

Fallback chain is now: iMessage → SMS → RCS.
