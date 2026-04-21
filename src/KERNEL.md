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

### `live/events/` directory

Events are plain files dropped into `events/`. The kernel watches this directory. When a file appears, the kernel reads it, acts, and deletes it (consumed).

```
live/events/
├── 1745267400-imessage-kaia.yaml
├── 1745267520-calendar-reminder.yaml
└── 1745268000-subagent-done.yaml
```

Filename format: `{unix-timestamp}-{source}-{slug}.yaml`

### Event schema

```yaml
source: imessage           # imessage | calendar | subagent | manual
type: message              # message | reminder | completion | notification
thread: kaia               # relevant thread/process slug
summary: "kaia: are we still on for dinner?"
```

### Event sources

Events are produced by external watchers — lightweight processes that bridge apps to the filesystem:

- **iMessage watcher** — detects new messages, writes event file
- **Calendar watcher** — fires on upcoming events
- **Subagent completion** — worker writes result + event file when done
- **Manual** — drop a YAML file into `events/` to trigger anything

The kernel doesn't know or care how events are produced. It just watches the directory.

## Process Directory (`proc/`)

```
live/proc/
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
type: plan                    # plan | follow-up | reminder | cron | subagent
spawned: 2026-04-21T14:00:00
deadline: 2026-04-22T20:00:00  # one-shot types
schedule: "0 0 * * *"          # cron types — standard cron expression
people:
  - kaia
  - engin
description: Dinner at gyro project tomorrow at 8
resolve_on: deadline           # deadline | confirmation | dependency | completion | schedule
depends_on: null               # another process slug, if dependency type
```

### status

Single word, no YAML. Read with `cat`, write with `echo >`.

### log.md

Append-only, same `[HH:MM]` format as messages:

```
[14:00] spawned from kaia/2026-04-21.md
[19:00] kernel: deadline in 1 hour, no confirmation seen
[20:30] kernel: deadline passed, marked expired
```

## Process Types

**plan** — Something with a deadline and people. "Dinner tomorrow at 8."
- Resolves: completed | expired | cancelled
- Kernel actions: schedule deadline wakeup, check for confirmation in messages, update people wikis on resolution, optionally spawn follow-up

**follow-up** — Triggered after another process resolves. "How was dinner?"
- Resolves: completed (follow-up sent and response received) | expired (window passed)
- Spawned by: kernel, when a parent plan completes or expires

**reminder** — Fire at a specific time. "Remind me to call mom Thursday."
- Resolves: completed (reminder delivered)
- Simplest type — just a deadline and a message

**cron** — PAI's internal recurring jobs. "Consolidate today's conversations at midnight."
- Never resolves — status stays `running` indefinitely (or `cancelled` to disable)
- `schedule` field instead of `deadline` — standard cron expression
- After each fire, kernel calculates next fire time and re-inserts into timer heap
- Examples: nightly consolidation, weekly stale-process sweep, periodic memory compaction

**subagent** — A spawned worker doing async work. "Research flights to Istanbul."
- Resolves: completed (subagent writes result to process dir)
- The subagent drops a file in `events/` when done; kernel wakes and resolves

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

```python
def spawn(slug, spec):
    proc_dir = LIVE_DIR / "proc" / slug
    proc_dir.mkdir(parents=True)
    write_yaml(proc_dir / "spec.yaml", spec)
    (proc_dir / "status").write_text("running\n")
    (proc_dir / "log.md").write_text(f"[{now()}] spawned\n")

    if spec.get("deadline"):
        heap_push(spec["deadline"], slug)
    elif spec.get("schedule"):
        next_fire = calc_next_cron(spec["schedule"])
        heap_push(next_fire, slug)
```

Spawning happens from:
1. **Extraction pipeline** (future) — identifies a plan in conversation, spawns process
2. **Manual** — `uv run python src/kernel.py spawn --type plan --deadline ...`
3. **The kernel itself** — spawning follow-ups when a plan resolves, or cron jobs on first boot
4. **Subagent dispatch** — PAI decides to delegate work, spawns a subagent process

## TODO

- **Context-limit compaction & session restart.** As PAI's conversation approaches the LLM's context window, the kernel is responsible for compacting the active session (summarize, dump state to `memory/`) and restarting the LLM with the compacted context. This is a kernel responsibility, not PAI's — PAI can't reliably reason about its own context budget while it's mid-thought. Likely shape: a monitor that tracks token usage per session, fires a `compact_needed` event past some threshold, kernel drives the summarize-and-restart flow. Revisit after Phase 3 (real `nudge()` with LLM calls).

## Resolved Processes

Resolved processes stay in `proc/` as a record. The kernel skips non-running statuses. Archive to `proc/.archive/` if the directory gets large.

## Implementation Order

1. Scaffold `proc/` and `events/` into `live/`, update `SCAFFOLDING.md`, `reset.py`
2. `src/kernel.py` — core async loop with `watchdog` FS watcher + timer heap
3. Event file reading/consumption
4. Timer handling — deadline resolution + cron rescheduling
5. `spawn()` function + CLI (`uv run python src/kernel.py spawn`)
6. Manual event injection for testing
7. Follow-up spawning on resolution
8. First cron job: nightly consolidation stub
9. iMessage watcher (first real event source)
