# PAI Kernel

## Philosophy

PAI is an LLM — it only thinks when prompted. The kernel is the nudge mechanism. Its job is to wake up when something happens (an event, a deadline, a schedule), construct the right context, and prompt PAI into action. Without the kernel, PAI only responds when a human talks to it. With it, PAI is an always-on agent.

The analogy is literal: `proc/` is `/proc`, the kernel manages process lifecycles like an OS kernel. The key difference from OpenClaw's polling approach: **this kernel is tickless**. It sleeps until something happens.

## Responsibilities

1. **Track human plans** — dinner tomorrow, basketball Thursday, flight next week. Each becomes a process in `proc/` with a deadline and people.
2. **Run PAI's own jobs** — consolidation, memory upserts, stale process sweeps, periodic check-ins. These are kernel-driven cron processes, not system crontabs. PAI's internal maintenance lives in `proc/` like everything else.
3. **Listen for notifications** — app events land as files in `events/`. The kernel wakes, evaluates, and nudges PAI.
4. **Subagent tracking** — when PAI spawns a worker (research, drafting, etc.), that's a process too. The subagent writes its result to the process dir; the kernel wakes on completion.

## Tickless Architecture

No polling. No system cron. The kernel is a long-lived process that sleeps until woken by one of two things:

1. **Filesystem event** — a file lands in `events/`. The OS notifies the kernel via `FSEvents`/`kqueue` (exposed through Python's `watchdog` library). Zero CPU while waiting.
2. **Timer expiry** — when a process has a deadline or schedule, the kernel sets an internal wakeup (via `asyncio.sleep` or a timer heap). When the earliest timer fires, the kernel wakes and evaluates.

The kernel wakes on **whichever comes first** — an event or a timer. Both are non-polling.

```
                    ┌─────────────┐
                    │   kernel    │
                    │  (sleeping) │
                    └──────┬──────┘
                           │
              ┌────────────┼────────────┐
              ▼            ▼            ▼
        FS event       timer         signal
     (events/ dir)   (heap)        (SIGHUP)
              │            │            │
              └────────────┼────────────┘
                           ▼
                    ┌─────────────┐
                    │  construct   │
                    │  context &   │
                    │  nudge PAI   │
                    └─────────────┘
```

### Why not poll?

Polling (scan proc/ every N seconds) wastes tokens and CPU on empty checks. Most ticks find nothing changed. A tickless kernel does exactly zero work between events — the OS handles the waiting for free.

### Timer scheduling

When a process is spawned with a deadline (or a cron process calculates its next fire time), the kernel inserts it into a timer heap (min-heap sorted by fire time). The sleep duration is always `earliest_timer - now`. When a process resolves or is cancelled, its timer is removed.

## Event System

### `home/events/` directory

Events are plain files dropped into `events/`. The kernel watches this directory. When a file appears, the kernel reads it, acts, and deletes it (consumed).

```
home/events/
├── 1745267400-imessage-alex.yaml
├── 1745267520-calendar-reminder.yaml
└── 1745268000-subagent-done.yaml
```

Filename format: `{unix-timestamp}-{source}-{slug}.yaml`

### Event schema

```yaml
source: imessage           # imessage | calendar | subagent | manual
type: message              # message | reminder | completion | notification
thread: alex               # relevant thread/process slug
summary: "alex: are we still on for dinner?"
```

### Event sources

Events are produced by external watchers — lightweight processes that bridge apps to the filesystem:

- **iMessage watcher** — detects new messages, writes event file
- **Calendar watcher** — fires on upcoming events
- **Subagent completion** — worker writes result + event file when done
- **Manual** — drop a YAML file into `events/` to trigger anything

The kernel doesn't know or care how events are produced. It just watches the directory.

### Inter-PAI messaging (`pai_message`, `subagent:response`)

PAIs talk to each other through the event bus. Two directed event kinds, both routed point-to-point via `target_pid` (no `wake_on` glob fan-out):

- **`pai_message`** — generic peer send_message, used in either direction by any PAI talking to any other PAI. Emitted by `bin/send-message --to <pid> --content "..."`. The spawn kickoff prompt also rides this channel — it's just the parent's first send_message to the newborn child.
- **`subagent:response`** — narrower kind for child→parent only. Emitted by `bin/subagent reply --content "..."` for intermediate updates, or by `bin/subagent done --result result.md` for final completion after the child writes its durable report under `$PAI_RESULT_DIR/`. The parent receives a nudge with `reason: subagent response` and can tell at a glance "this is from one of my own children" without inspecting the sender's spec.

A spawned subagent has `persistent: true` in its spec, so it stays alive across turns and only resolves when the parent calls `bin/subagent kill --slug <name>`. Until then, parent and child can exchange any number of messages. This is why a parent can drive N concurrent subagents without blocking — every turn is mediated by the bus, not by a synchronous call.

### PAI turn events (`pai:<slug>:input`, `pai:<slug>:output`)

The kernel emits two events around every nudge so other PAIs can react to a peer's turns declaratively, without tail/peek primitives:

- **`pai:<slug>:input`** — fires before the LLM runs, after the target slug/pid is resolved. Payload includes `reason` and (when present) the originating event/context as `trigger`.
- **`pai:<slug>:output`** — fires after the assistant reply is committed to `proc/<slug>/messages.jsonl`. Pointer-style payload (`turn_index`, `messages_path`); subscribers re-read the file to get the actual content.

Listeners subscribe in `/etc/config.yaml` via `wake_on: [pai:main:output]` (target a specific slug — the wildcard form `pai:*:output` would self-trigger the listener on its own turns). The full kind list lives in `KERNEL_EVENTS.md`.

## Process Directory (`proc/`)

```
home/proc/
├── dinner-gyro-project/
│   ├── spec.yaml
│   ├── status
│   └── log.md
├── remind-call-mom/
│   ├── spec.yaml
│   ├── status
│   └── log.md
└── nightly-consolidation/      # PAI's own cron job
    ├── spec.yaml
    ├── status
    └── log.md
```

### spec.yaml

```yaml
# Background service (forks immediately, supervised until exit or cancel)
run: bin/subagent "research flights to istanbul"
restart: never                     # never | on-failure | always (default: never)
deadline: 2026-04-22T15:00:00      # optional; kernel auto-expires and kills subprocess
spawned: 2026-04-22T14:00:00       # stamped by paicron
description: "Research flights"    # optional; free text for humans
people: [alex]                     # optional; related people

# Cron / timer service (fires on schedule)
schedule: "0 9 * * *"              # cron expr (recurring) OR ISO datetime (one-shot)
run: bin/morning-briefing          # optional; absent means "nudge PAI on fire"
restart: always                    # applies to each per-fire subprocess
```

### status

Single word, no YAML. Read with `cat`, write with `echo >`. Values: `spawned | running | scheduled | completed | expired | cancelled | failed | stopped`.

`running` and `scheduled` are the two *active* (non-terminal) statuses. `running` = a live background subprocess (or a PAI). `scheduled` = an armed timer resting in the kernel's heap with nothing executing — a cron between fires, or a deadline/one-shot waiting to fire. The kernel keys re-arm, fire, and shutdown-preserve decisions off the active set, not `running` alone, so an armed cron need not masquerade as a live process to stay scheduled.

### log.md

Append-only, same `[HH:MM]` format as messages:

```
[14:00] spawned
[14:00] kernel: subprocess started pid=24901 (bin/subagent 'flights')
[14:03] stdout: {"status": "ok", "top": "THY 1234"}
[14:03] kernel: subprocess exited rc=0
[14:03] kernel: resolved as completed
```

## Service shapes

There is no `type:` field. Shape is determined by which fields are present:

**Background service** — has `run:`, no `schedule:`. Kernel forks the command immediately, tees stdout/stderr into `log.md`, and supervises until exit or cancel. On exit, kernel resolves the proc (`completed` on rc=0, `failed` on non-zero) and emits an event that nudges PAI. Examples: research subagents, inbox watchers, long HTTP polls.

**Reminder** — has `schedule:`, no `run:`. Kernel arms a timer; the proc rests at `scheduled`. On fire: nudges PAI with `reason: schedule fired`. One-shot schedules resolve `completed` after firing; cron expressions stay `scheduled` (kernel re-arms the next fire).

**Cron job** — has both `schedule:` and `run:`. On each fire, kernel launches a *transient* per-fire subprocess whose output is logged; the parent proc stays `scheduled` across fires (it's an armed timer, not a live service). Use for PAI's internal recurring jobs (nightly consolidation, stale-process sweep).

**Deferred background service** — `schedule:` is a one-shot ISO datetime and `run:` is set. The proc rests at `scheduled` until fire time, when the kernel starts the subprocess under supervision and flips it to `running` (same as a plain background service, just delayed).

**Deadline-only** — has `deadline:`, no `schedule:`, no `run:`. The kernel auto-expires the proc at the deadline and nudges PAI. Deprecated shape — prefer `schedule:` with an ISO datetime for timed reminders; keep `deadline:` for capping the runtime of a running service.

### Restart policy

`restart: never` (default) — subprocess exit resolves the proc. `on-failure` re-forks on non-zero exit; `always` re-forks on every exit. Kernel restart counts as an implicit failure, so `on-failure`/`always` procs resume across kernel bounces; `never` procs get marked `failed` with a log line on boot.

## Kernel Loop

```python
async def run():
    """Main kernel loop — sleep until event or deadline."""
    heap = load_timer_heap()          # min-heap of (fire_time, proc_slug)
    watcher = watch_events_dir()      # async generator, yields on new file

    while True:
        timeout = time_until_next_timer(heap)

        event = await wait_for_either(
            watcher.next(),            # FS event
            sleep(timeout),            # timer
        )

        if event.is_fs:
            handle_event(event.file)
        elif event.is_timer:
            handle_timer(heap.pop())

        # After any wakeup, drain all elapsed timers
        while heap and heap[0].fire_time <= now():
            handle_timer(heap.pop())
```

### Event handling

```python
def handle_event(event_file):
    event = read_yaml(event_file)
    event_file.unlink()  # consumed

    match event["type"]:
        case "message":
            # Check if any running process cares about this thread
            check_confirmations(event)
            # Trigger extraction/consolidation if needed
        case "completion":
            # Subagent finished — resolve its process
            resolve(event["thread"], "completed")
        case "reminder":
            # Calendar event approaching
            find_or_spawn_reminder(event)
        case "notification":
            # Generic — log and evaluate
            evaluate_notification(event)
```

### Timer handling

```python
def handle_timer(entry):
    proc_slug = entry.proc_slug
    spec = read_spec(proc_slug)

    if spec["type"] == "cron":
        # Fire the job, then reschedule
        nudge_pai(proc_slug, spec)
        next_fire = calc_next_cron(spec["schedule"])
        heap_push(next_fire, proc_slug)
        append_log(proc_slug, f"kernel: fired, next at {next_fire}")
    else:
        # One-shot deadline
        handle_deadline(proc_slug, spec)
```

### Resolution

```python
def resolve(proc_slug, new_status):
    write_status(proc_slug, new_status)
    append_log(proc_slug, f"kernel: resolved as {new_status}")

    spec = read_spec(proc_slug)

    # Side effects
    if new_status == "completed":
        update_people_wikis(spec)
        if spec["type"] == "plan":
            spawn_follow_up(spec)

    # Remove from timer heap
    remove_from_heap(proc_slug)
```

## Spawning

Use `bin/paicron start` — the systemctl-shaped frontend. It writes the three files and hands off to the kernel via filesystem watch. No send_message.

```
bin/paicron start --slug research-flights \
    --run "bin/subagent 'flights to istanbul'" \
    --restart never
```

`paicron` appends `-YYYY-MM-DD` to the slug automatically (falls back to full timestamp on same-day collision). Under the hood it just calls `processes.spawn()`:

```python
def spawn(slug, spec):
    proc_dir = HOME_DIR / "proc" / slug
    proc_dir.mkdir(parents=True)
    write_yaml(proc_dir / "spec.yaml", spec)
    (proc_dir / "status").write_text("running\n")
    (proc_dir / "log.md").write_text(f"[{now()}] spawned\n")
```

The kernel's `proc_watcher` picks up the new directory and:
- Arms the timer heap if `deadline:` or `schedule:` is present.
- Hands to the supervisor if `run:` is present without a `schedule:` (background service).

Spawning happens from:
1. **PAI itself** — runs `bin/paicron start ...` from its shell when it needs async work or a timed reminder.
2. **The owner / humans** — same command, same surface.
3. **The kernel itself** — spawning follow-ups when a service completes, or seeding internal cron jobs on first boot.

## Shipped (formerly TODO)

- **Context-limit compaction & session restart — DONE.** The kernel tracks
  per-PAI token usage and drives compaction: a soft threshold asks the PAI to
  call `bin/compact`, and a kernel-enforced hard backstop compacts the history
  itself when the soft path is ignored. Provider context-window overflow is
  recovered by archiving the oversized history and retrying the turn. See
  `src/boot/nudge.py`.

- **Session persistence across nudges — DONE.** Per-PAI conversation history
  lives in `proc/<pai>/messages.jsonl`; it is loaded, threaded through the
  turn, and persisted on completion, so reasoning survives between wakes. The
  design rule still holds — **session ≠ read cache**: the session carries
  reasoning while file reads re-run every wake, since the filesystem may change
  externally between nudges.

## Resolved Processes

Resolved processes stay in `proc/` as a record. The kernel skips non-running statuses. Archive to `proc/.archive/` if the directory gets large.

## Implementation Order

1. Scaffold `proc/` and `events/` into `home/`, update `SCAFFOLDING.md`, `reset.py`
2. `src/kernel.py` — core async loop with `watchdog` FS watcher + timer heap
3. Event file reading/consumption
4. Timer handling — deadline resolution + cron rescheduling
5. `spawn()` function + CLI (`uv run python src/kernel.py spawn`)
6. Manual event injection for testing
7. Follow-up spawning on resolution
8. First cron job: nightly consolidation stub
9. iMessage watcher (first real event source)
