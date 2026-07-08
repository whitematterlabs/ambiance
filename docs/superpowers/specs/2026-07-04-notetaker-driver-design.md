# Notetaker Driver — Local Call Recording + Transcription (v1)

**Status:** Implemented (2026-07-07) — see `docs/superpowers/plans/2026-07-07-cowork-notetaker-rollout.md`. Implementation deviations: raw PCM (`mic.raw` int16 + `system.raw` float32, ffmpeg-mixed at finalize) instead of `.caf` — crash-safe; tap IO reads via ctypes HAL IOProc (pyobjc can't round-trip `AudioDeviceIOProcID`); auto-stop-on-silence not implemented (manual stop only).
**Date:** 2026-07-04

## Context

A sibling to Cowork Mode (see `2026-07-03-cowork-window-activity-design.md`): a
driver that records a video/phone call locally, transcribes it, and hands PAI a
transcript so PAI can write up notes and action items. It shares cowork's
ambient-observational posture but is deliberately *not* part of the `cowork`
driver and *not* on by default — because it records **other people**, which is a
categorically heavier consent and legal story than watching the owner's own
window titles.

The elegant local approach — the one that fits PAI — is **not** a bot that joins
the meeting. It's capturing the Mac's own audio (the owner's mic + the other
participants coming out of the speakers), mixing, and transcribing on the owner's
machine. That's platform-agnostic (Zoom, Meet, Teams, a phone call on speaker,
anything making sound), invisible to other participants at the protocol level,
and needs no per-platform bot credentials.

Two deliberate v1 decisions frame everything below:

- **Manual trigger.** The owner explicitly tells PAI to take notes on *this*
  call. That act is the consent boundary; there is no auto-detection in v1.
- **Local-default transcription, cloud opt-in.** Audio is transcribed on-device
  by default so other participants' audio never leaves the Mac; the owner can
  opt a given call up to cloud STT for a better transcript when they judge the
  tradeoff acceptable. This default is flippable if local proves too rough.

## Goals

- Capture system audio output + mic locally and mix them into one recording,
  starting/stopping on explicit owner instruction.
- Transcribe the recording via PAI's existing STT dispatch (the `voice` /
  `voice_cloud` split), local by default, cloud per-call opt-in.
- Emit a `notetaker:transcript_ready` event so PAI's normal reasoning loop writes
  the human-facing summary + action items — the driver captures and transcribes;
  PAI (the LLM) does the summarizing.
- Be honest and visible while recording: an in-console indicator plus PAI
  announcing start/stop in chat, on top of the macOS system indicators.
- Default to *not retaining raw audio* — keep the transcript and summary, delete
  the audio once transcription succeeds.

## Non-goals (v1)

- **Auto-detecting calls** (mic hot / call-app frontmost / calendar link) —
  deliberately manual; auto-start records third parties with no human in the
  loop. Deferred (see Future work).
- **Diarization** (who-said-what) — v1 produces a single merged transcript.
- **Live / streaming notes during the call** — v1 transcribes on stop.
- **A bot that joins the meeting** — rejected in favor of local audio capture.
- **Video / screen capture** — audio only.
- **Virtual audio drivers** (BlackHole/Loopback) — rejected; installing an audio
  kernel driver is exactly the "install breaks setup" trap PAI has been burned by
  (`PAI.app` deletion, `ax` prebuilt-sidecar lesson).
- **Obtaining other participants' consent** — PAI records on the owner's
  instruction and stays visible about it; disclosing to the room is the owner's
  responsibility (see Consent).

## Architecture

New driver at `~/Projects/pairegistry/drivers/notetaker/`, sibling to `cowork`:

- `package.yaml` — paiman manifest.
- `events.yaml` — declares process `recorder` with:
  - actions `notetaker:start` / `notetaker:stop` (owner-triggered, following the
    action pattern the `email` driver already uses for `action:send`), each
    accepting an optional `cloud: true` to opt this session up to cloud STT.
  - event kind `notetaker:transcript_ready`.
- `recorder.py` — pure Python via `pyobjc`. Capture uses **Core Audio process
  taps** (`AudioHardwareCreateProcessTap` + `CATapDescription`, macOS 14.4+) to
  read system audio output with no virtual device and no kernel extension, plus
  `AVAudioEngine`'s input node for the mic. The two streams are mixed and written
  to a session file as the call proceeds. Owner is on macOS 26, so process taps
  are available; **no prebuilt Swift sidecar is needed** (unlike `ax`) — pyobjc
  drives both APIs — keeping us on the right side of the no-Swift lesson.

Capturing system audio via process taps requires **Screen Recording TCC** (Apple
bundles audio capture under it). This is the only new permission — no Full Disk
Access, no Accessibility.

## Data flow

1. Owner tells PAI "take notes on this call." PAI invokes `notetaker:start`
   (optionally `cloud: true`).
2. `recorder.py` verifies the capability flag and Screen Recording grant, then
   opens the process tap (system output) + `AVAudioEngine` (mic), mixes, and
   streams audio to `/sys/drivers/notetaker/sessions/<session-id>/audio.caf`.
   It raises the in-console recording indicator and PAI announces it's recording.
3. Owner says stop (or the driver auto-stops when the call app quits / the audio
   goes silent for a while — a convenience; the manual stop is authoritative).
   `recorder.py` finalizes the audio file.
4. It transcribes: local STT dispatch by default, `voice_cloud` if the session
   was started with `cloud: true`. Output is a merged transcript (no speaker
   labels in v1) written to `.../sessions/<session-id>/transcript.json`.
5. On success it deletes `audio.caf` (privacy default — the raw audio of other
   people is not retained) and emits `notetaker:transcript_ready` with the
   session id and transcript path. If transcription *fails*, the audio is kept so
   it can be retried.
6. The kernel routes the event to the owner's PAI. PAI reads the transcript and
   writes a human summary + action items to a markdown file in its home,
   `~/<pai>/notes/calls/<date>-<slug>.md`. The driver owns capture+transcription
   (the recording surface's on-disk shape); PAI owns the summary (reasoning) —
   the same split as everywhere else.

No cursor/backlog — a recording is a live session, not a replay of an external
store.

## Capability gating, consent & privacy

- New capability flag `notetaker` in `CAPABILITY_SPECS` (`src/boot/config.py`),
  tri-state, **default `no`** — the opposite of `cowork`'s default-`yes`. Because
  it records third parties, it fails closed: the owner must explicitly enable it
  once before any call can be recorded.
- **Two-tier consent.** Tier 1: enabling the capability once (a deliberate opt-in
  that surfaces the owner's responsibility for recording others, incl.
  two-party-consent jurisdictions, and walks them through the Screen Recording
  grant). Tier 2: the per-call manual `notetaker:start` — recording only ever
  happens on an explicit, per-session owner instruction, never ambiently.
- **Visible while recording.** The web console shows a clear "recording this
  call" state, and PAI announces start/stop in chat — on top of the macOS system
  indicators (orange mic dot, Screen Recording menubar indicator). PAI never
  records silently.
- **PAI does not obtain other participants' consent** and does not pretend to;
  disclosing to the room is the owner's call. The visibility above exists so the
  owner is never unaware PAI is recording and can disclose.
- **Raw audio is not retained** by default — deleted on successful transcription.
  Transcript + summary are kept. (Keeping audio is a future opt-in.)
- The flag feeds the `<capabilities>` prompt block, so PAI's self-description
  discloses that it can record and transcribe calls when enabled — enforcement
  and disclosure stay in sync.

## Output format

Driver-owned, per session under `/sys/drivers/notetaker/sessions/<session-id>/`:

- `audio.caf` — the mixed recording (transient; deleted after transcription).
- `transcript.json` — `{session_id, started, ended, cloud: bool, segments:
  [{start, end, text}]}`. No `speaker` field in v1.

PAI-owned, per call in the PAI's home:

- `~/<pai>/notes/calls/<date>-<slug>.md` — human summary + action items PAI
  writes from the transcript. Plain markdown, greppable, per filesystem dogma.

## Error handling

- **Screen Recording not granted** → `recorder.py` refuses to start, emits a
  single clear message telling the owner to grant it, and does not create a
  session. Recording never silently no-ops.
- **Mic unavailable/denied** → warn and record system audio only (captures the
  other participants but not the owner) rather than failing outright; the
  transcript notes the owner's side is missing.
- **Transcription failure** → keep `audio.caf`, mark the session failed, surface
  it so the owner can retry (local↔cloud); never silently drop a recorded call.
- **Crash mid-session** → finalize whatever audio was written and attempt
  transcription on the partial file rather than discarding it.

## Testing

Manual verification only for v1:

1. Enable the `notetaker` capability; grant Screen Recording.
2. Start a call (or play audio + speak into the mic); tell PAI to take notes.
3. Confirm the console shows the recording indicator and PAI announces it.
4. Stop; confirm a `transcript.json` is produced, `audio.caf` is deleted, a
   `notetaker:transcript_ready` event fires, and PAI writes a summary markdown
   into its home.
5. Repeat with `cloud: true` and confirm the cloud path is used.
6. Deny Screen Recording and confirm start refuses cleanly with a clear message.

No automated suite for v1 — it's OS-level audio capture, not logic.

## Future work (explicitly deferred)

- **Auto-detect + ask-first trigger** — detect a call (mic hot / call-app
  frontmost / calendar link via the calendar driver) and prompt the owner before
  recording. The middle-ground trigger, held until the manual flow is proven.
- **Diarization** — speaker labels, best via cloud STT.
- **Live streaming transcript/notes** — transcribe during the call, not on stop.
- **Calendar linkage** — attach notes to the calendar event and attendees.
- **Configurable retention** — opt in to keeping raw audio.
- **Flip the STT default to cloud** if local transcription proves too weak in
  practice.
