# Kernel events

The kernel is event-driven. Drivers, CLI tools, and the kernel itself
drop YAML files into the event spool; the supervisor (`boot/main.py`)
reads them in arrival order, routes by `kind`, and either dispatches a
nudge or handles the event in-band.

This file documents (a) the model-facing `reason` strings a PAI sees on a
nudge and how to handle each, (b) where events live on disk, (c) the YAML
schema of an event file, (d) the full kind taxonomy with emitter/consumer
pairs, and (e) backfill behavior after kernel downtime.

## Nudge reasons (what a PAI sees)

When the kernel wakes a PAI it sets a `reason` on the turn. The on-disk
`kind` taxonomy below is the emitter's view; this section is the receiver's.
Default handling for each:

- `owner message` — incoming message. Read the thread, decide whether to reply.
- `proc completed` / `proc failed` / `proc expired` — a service you (or the
  kernel) started has finished; the event's `slug` names it. Read
  `proc/{slug}/log.md` if present; for a finished subagent, its report and
  artifacts are handed off under `workspace/{slug}/` (e.g.
  `workspace/{slug}/result.md`) — its own `proc/{slug}/` is already reaped.
  Then produce a short summary as your assistant reply (the kernel posts it
  to the me/ thread for you); include the outcome and, for failures, the
  reason if obvious. Suppress the summary only if the service is internal
  maintenance (nightly consolidation, sweeps) and nothing notable happened —
  call `stand_down` instead of a one-line filler reply. Do NOT echo the summary
  into the me/ thread yourself.
- `schedule fired` — a timed reminder fired (schedule with no `run:`).
  Surface it to the owner if the reminder was meant for them; otherwise do
  whatever the reminder asked for.
- `cron fired (rc=N)` — a cron-with-run service's per-fire subprocess
  finished. Check the log for its output. Summarize to the owner only when
  the result is actionable, surprising, failed, or otherwise notable (the
  kernel posts it for you — don't echo it). For successful high-frequency or
  purely-internal crons with nothing notable, call `stand_down` — the owner can
  set `announce: false` on the spec to suppress the nudge entirely.
- `deadline reached` — a service hit its deadline without completing.
  Investigate and report.
- `send failed` — an outbound message couldn't be delivered (e.g. the
  recipient isn't on iMessage and SMS relay is unavailable). Context has
  `thread`, `text`, and `reason`. Tell the owner so they can follow up
  manually; the line you wrote is still in the thread file but was never
  sent. Don't silently retry — the cursor already advanced.
- `nudge failed` — another PAI's turn raised before producing a reply (LLM
  API error, credit outage, transport bug). You receive this only if you are
  root. Context has `target` (slug), `target_pid`, `original_reason` (what
  they were being nudged for), and `error` (the exception repr). The kernel
  does not retry — the original event is gone. Decide whether to tell the
  owner, re-nudge the target later, or just note it and move on.

## Where events live

```
$PAI_ROOT/run/pai/events/{YYYYMMDDTHHMMSS<usec>}-{source}.yaml
```

Defined in `boot/paths.py` as `EVENTS_DIR = PAI_ROOT / "run" / "pai" / "events"`.
This is the only spool. Earlier drafts of this doc referenced
`home/events/` — that path was retired with the v3 FHS migration. Code
reads `paths.EVENTS_DIR`.

Sibling spool: `$PAI_ROOT/run/pai/acks/{msg_id}.yaml`. Per-message
delivery acks for `send-message`. Lives *outside* the event spool so
`EventWatcher` does not consume them; senders poll the ack path
directly. See `processes.emit_ack`. When the target has a turn running,
the message is injected into that turn at its next tool boundary
(`boot/inject.py`) and the ack carries `delivery: injected`; otherwise
the ack is written when the queued nudge starts.

### Write protocol

`processes.emit_event(payload, target_pid=None)` is the only sanctioned
writer. It:

1. Ensures the spool exists.
2. Stamps `target_pid` onto the payload if given.
3. Names the file `{microsecond-timestamp}-{source}.yaml`.
4. Writes to `*.yaml.tmp` and `os.replace`s into place — one atomic
   CREATE for watchdog, never a partial read.

### Read protocol

`boot/events.py:EventWatcher` runs a watchdog observer on the spool.
On `start()` it enqueues any files already on disk (boot catch-up) and
then watches for `on_created` / `on_moved`. `read_event(path)` parses
the YAML and **unlinks the file** — events are consumed exactly once.
A 5-second seen-path cache absorbs FSEvents redelivery.

## Event file schema

Every event is a single YAML mapping. There is no enforced schema, but
two keys are conventional:

| key | purpose |
|---|---|
| `source` | Emitter identity. `kernel`, `pai`, `tui`, `send-message`, `reboot`, `paictl`, `paiadd`, `paidel`, `paiman`, or a driver name (`imessage`, `email`, `voice`, `ax`, `whatsapp`, `calendar`, …). |
| `kind` | What the event *is*. Plain word (`new_message`, `interrupt`, `proc_resolved`, `cron_fired`) for kernel-known events, or `<source>:<bare>` for generic driver events (`voice:utterance`, `ax:keystroke`). |

Optional routing keys:

| key | meaning |
|---|---|
| `target_pid` | Bypass `wake_on` matching; deliver to exactly this PAI pid. Used by drivers with per-PAI session state (e.g. `ax`) and by the boot-time backfill collapser. |
| `parent` | Pid to escalate to. Set on `proc_resolved` from the resolving spec; used by subagent return-path routing. |

All other keys are payload, opaque to the kernel; they flow into the
nudge as `context` and become part of the user-turn prompt.

## Routing model

For each event, `boot/main.py:_handle_event_file` dispatches in this
order:

1. **Kernel-known `kind`** — explicit branch in `_handle_event_file`
   (see taxonomy below).
2. **`pai:*` kinds** — match every running PAI's `wake_on` globs.
   Listener-only; no fallback to root, to break self-trigger loops on
   a PAI's own `:output`.
3. **`<source>:<kind>` generic** — when an event has a non-kernel
   `source` and a `kind`, the public kind becomes `{source}:{kind}` and
   is fed to `routing.route_to_pids`. If `target_pid` is set it bypasses
   `wake_on` and delivers to that pid only.
4. **Fallback** — nudge `parent` (default pid 1) with `event: {kind}`.

`routing.route_to_pids(kind)` walks every running `kind:pai` proc; a
PAI subscribes by listing fnmatch globs under `wake_on:` in
`/etc/config.yaml` or its spec. If nothing matches, the fleet's
fallback PAI receives the event.

## Kind taxonomy

### Kernel control plane

| kind | emitter | consumer | payload |
|---|---|---|---|
| `kernel:reload_config` | `paictl`, `paiadd`, `paidel`, `paiman` | kernel (`_handle_reload_config`) | none (side effects only) |
| `kernel:restart` | `/sbin/reboot`, `paictl` | kernel (`_handle_restart`); entry.py execs after run() returns | none |
| `kernel:backfill` | `boot/phases/backfill.py` | one PAI by `target_pid` (no wake_on) | `target_pid`, `count`, `by_kind`, `manifest_glob`, `window` |
| `kernel:reload_failed` | kernel (`_handle_reload_config` on exception) | wake_on: `kernel:reload_failed` | `error`, `traceback` |
| `interrupt` | TUI (ESC) | kernel — cancels all in-flight nudges for `pai` | `pai: <int>` |

### Process lifecycle

| kind | emitter | consumer | payload |
|---|---|---|---|
| `proc_resolved` | `processes.resolve` on `completed`/`expired`/`failed` | parent pid (if set in spec); else pid 1 only on `failed`/`expired` for self-healing | `slug`, `status`, `parent?` |
| `cron_fired` | `boot/supervisor.py` after a cron `run:` exits (when `announce: true`) | parent pid (default 1) | `slug`, `rc`, `parent?` |

### iMessage (driver: `imessage`, special-cased in kernel)

| kind | emitter | consumer | payload |
|---|---|---|---|
| `new_message` | `drivers/imessage/inbound.py`, also TUI (`source: tui`, `thread: me`) | `imessage:new` listeners; TUI variant goes to `target_pid` or `imessage:owner` | `handle`, `text`, `chat_guid?`, `display_name?`, `received_at?`, `is_from_me?`, `chat_handles?`, `source` |
| `messages_backlog` | imessage driver on boot catch-up | `imessage:backlog` listeners | `messages: [...]` |
| `messages_multiple` | imessage driver on live burst | `imessage:multiple_messages` listeners | `messages: [...]` |
| `send_failed` | imessage outbound | `imessage:send_failed` listeners | `thread`, `text`, `reason` |

### Email (driver: `email/macmail`)

| kind | emitter | consumer | payload |
|---|---|---|---|
| `new_email` | macmail inbound | `email:new` listeners | `account`, `thread_slug`, `subject`, `from`, `direction`, `path` |
| `email_backlog` | macmail inbound (boot) | `email:backlog` listeners | `since`, `accounts`, `total` |
| `draft_failed` | macmail outbound | `email:draft_failed` listeners | `account`, `path`, `reason` |

### Inter-PAI

| kind | emitter | consumer | payload |
|---|---|---|---|
| `pai_message` | `bin/send-message`, `bin/subagent` | `target_pid` (direct delivery) | `target_pid`, `sender_pid`, `text`, `msg_id?` |
| `subagent:response` | `bin/subagent` | `target_pid` (parent) | `target_pid`, `sender_pid`, `text`, `done?`, `result?` |
| `subagent:plan_ready` | `bin/subagent` | `target_pid` | `target_pid`, `sender_pid`, `slug`, `text?` |
| `subagent:plan_reject` | `bin/subagent` | `target_pid` | `target_pid`, `sender_pid`, `slug`, `text?` |

### PAI turn announcements

Emitted by `boot/nudge.py` on every PAI turn. Listener-only — no
fallback to root, by design (root would self-nudge on its own turn and
loop).

| kind | when | payload |
|---|---|---|
| `pai:<slug>:input` | before LLM runs | `slug`, `pid`, `reason`, `trigger?` |
| `pai:<slug>:output` | after assistant reply committed to `messages.jsonl` | `slug`, `pid`, `turn_index`, `messages_path` |

**Loop hazard.** `wake_on: [pai:*:output]` self-triggers — the
listener's own turn matches the glob. Target a specific slug
(`pai:main:output`) unless you genuinely want fleet-wide fan-out and
have a re-entry guard.

### Generic driver events

Any driver emits `{source: <name>, kind: <bare>}` and the kernel
synthesizes the public kind `<name>:<bare>` for `wake_on` matching.
No kernel patch required. Examples currently in tree:

- `voice:utterance`, `voice:wake_failed` (drivers/voice)
- `ax:keystroke` and friends (drivers/ax, uses `target_pid`)
- `whatsapp:new_message`, `whatsapp:send_failed` (drivers/whatsapp)
- `calendar:item_added` (drivers/calendar)

Subscribe by listing the public kind in a PAI's `wake_on:`.

## Backfill after downtime

When the kernel has been down long enough for upstream drivers to back
up — overnight, days — the spool can contain hundreds of events. A
naive catch-up would dispatch each as a separate LLM turn.

`boot/phases/backfill.py` runs **before** `EventWatcher.start()` and:

1. Scans `EVENTS_DIR`. If file count ≤ `THRESHOLD` (10), exits — let
   the watcher dispatch normally.
2. Groups events by primary target pid (first hit from
   `routing.route_to_pids` on the synthesized public kind). iMessage
   and email special-cases are *not* modeled; those fall through to
   the fallback PAI, which is where they would have gone anyway.
3. For each pid with > THRESHOLD events:
   - Emits a single `kernel:backfill` event with `target_pid`,
     `count`, `by_kind` histogram, `manifest_glob`, and time `window`.
   - Moves the originals into
     `$PAI_ROOT/var/log/events/backfill/{boot-ts}/pid-{pid}/`.
4. Crash-safe ordering: the synthetic event is written first
   (atomic via `emit_event`). If we die mid-archive, the leftover
   originals join a future backfill or dispatch normally — nothing is
   silently dropped.

The receiving PAI wakes exactly once with the summary and drills into
the archive only where it cares.
