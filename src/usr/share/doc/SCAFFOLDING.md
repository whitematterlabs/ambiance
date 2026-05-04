# PAI Scaffolding

> **Status: v1, superseded.** This document describes the *current* on-disk
> layout. The forward-looking target is `FILESYSTEM.md`, an FHS-aligned
> redesign (per-PAI homes under `/home/<pai>/`, canonical state in
> `/var/lib/`, kernelPAI in `/root/`, etc.). Treat this file as the
> source of truth for what *exists today*; treat `FILESYSTEM.md` as the
> source of truth for where we're going. Do not extend this layout — new
> structural decisions should be made against `FILESYSTEM.md`.

## Repo layout

```
src/         # agent source code
etc/         # kernel control plane (agent-readable via home/etc symlink)
  config.yaml                # long-running PAI fleet declaration
  drivers/{driver}/events.yaml  # per-driver event manifest
packages/    # reusable PAI/skill/driver bundles (only kind: pai is honored in v1)
home/        # agent's runtime workspace (see below)
```

`etc/config.yaml` is reconciled against `home/proc/` at boot and on a
`kernel:reload_config` event. See `src/kernel/config.py` for the schema.

`usr/lib/drivers/{driver}/events.yaml` enumerates the event-kinds each
driver emits — their `wake_on` routing keys, raw event-file kinds,
emitter source paths, and payload shapes. Single source of truth for
both PAI (writing `wake_on:` patterns) and humans (debugging routing).

## Philosophy

Everything is a filesystem. The agent navigates its world using standard shell primitives (`ls`, `cat`, `grep`, `find`, `tail`, `echo >>`). No custom APIs, no blind graph traversal. Relationships are symlinks. Data is plain text.

## Live Directory Structure

```
home/
├── communication/
│   └── messages/                        # iMessage
│       ├── me/                          # PAI ↔ owner channel
│       │   ├── meta.yaml
│       │   └── 2026-04-20.md
│       ├── {contact-name}/              # 1:1 thread
│       │   ├── {contact-name} -> ../../../memory/people/{contact-name}/
│       │   ├── meta.yaml                # thread metadata
│       │   ├── 2026-04-18.md            # messages from that day
│       │   ├── 2026-04-19.md
│       │   └── 2026-04-20.md
│       └── {group-name}/               # group chat
│           ├── {member-name} -> ../../../memory/people/{member-name}/
│           ├── meta.yaml
│           ├── 2026-04-18.md
│           └── 2026-04-20.md
├── memory/
│   ├── myself/                          # self-knowledge
│   │   ├── identity.yaml                # name, age, location — elementary facts
│   │   └── directives.md                # owner-defined behavioral instructions
│   ├── people/
│   │   └── {contact-name}/
│   │       └── about.yaml               # structured profile + freeform wiki entry
│   ├── topics/                          # first-class topic entities
│   │   └── {topic-name}/
│   │       ├── meta.yaml                # topic name, status, related people
│   │       ├── 2026-04-15/
│   │       │   ├── alice-smith.md -> ../../../../communication/messages/alice-smith/2026-04-15.md
│   │       │   └── summary.md
│   │       └── 2026-04-18/
│   │           ├── weekend-crew.md -> ../../../../communication/messages/weekend-crew/2026-04-18.md
│   │           └── summary.md
│   ├── journal/                         # daily aggregation — everything that happened
│   │   ├── 2026-04-18/
│   │   │   ├── alice-smith.md -> ../../../communication/messages/alice-smith/2026-04-18.md
│   │   │   ├── weekend-crew.md -> ../../../communication/messages/weekend-crew/2026-04-18.md
│   │   │   └── notes.md                # agent's own reflections, summaries
│   │   ├── 2026-04-19/
│   │   └── 2026-04-20/
│   └── skills/                          # things the agent knows how to do
├── proc/                                # running services (kernel-managed)
│   └── {service-slug}/
│       ├── spec.yaml                    # service definition (run: and/or schedule:)
│       ├── status                       # spawned | running | completed | expired | cancelled | failed
│       └── log.md                       # append-only activity log + subprocess output
├── bin/                                 # executables
│   ├── paicron                          # service control (systemctl-shaped)
│   ├── paictl                           # PAI instance lifecycle (active flag)
│   └── {tool-name}                      # sync tools PAI runs inline
├── events/                              # kernel inbox — consumed on read
│   └── {timestamp}-{source}.yaml        # one event per file
├── etc -> ../etc/                       # kernelspace control plane (symlink)
├── tmp/                                 # ephemeral file storage
│   └── drivers/                         # outbound driver state (cursors, etc.)
│       └── {driver-name}/
│           └── cursors.yaml             # {relative-path: byte-offset}
└── workspace/                           # persistent file storage
```

## Message Format

Each day's file (e.g. `2026-04-18.md`) is a chronological, append-only message log:

```
[14:32] alice: hey are you coming tonight
[14:33] me: yeah probably around 8
[14:33] alice: perfect, bring that hot sauce lol
[14:35] me: obviously
```

- One message per line
- Timestamps in `[HH:MM]` (date is already in the filename)
- Sender is lowercase name or `me`
- New messages always append to today's date file
- Grepable, tailable, appendable

## The `me/` thread

`communication/messages/me/` is the direct channel between PAI and the owner. The agent writes here when it needs to surface something to the owner (reminders, follow-ups, questions, proactive notes), and the owner writes here to talk to PAI directly.

Same file format as any other thread. Sender is `pai` for the agent and `me` for the owner:

```
[09:00] pai: heads up — alice's birthday is friday, want me to draft something?
[09:02] me: yeah, keep it short
```

No participant symlinks — the thread is between the agent and `memory/myself/`.

## Symlink Conventions

Symlinks express relationships without duplicating data:

- Each thread folder symlinks its participants back to `memory/people/{name}/`
- The symlink name is the contact's full name (e.g. `alice-smith`)
- From inside a thread, the agent can `cat alice-smith/about.yaml` to recall who they are
- Writing to the symlink target updates it everywhere

## Agent Operations

| Action | Command |
|--------|---------|
| Check today's messages | `cat 2026-04-20.md` |
| See recent days | `ls *.md` |
| Search all history | `grep "hot sauce" *.md` |
| Read about a person | `cat alice-smith/about.yaml` |
| Update person knowledge | `echo "..." >> alice-smith/about.yaml` |
| List all contacts | `ls memory/people/` |
| List all threads | `ls communication/messages/` |
| Find threads with someone | `find communication/messages -type l -name "alice-smith"` |

## myself/

The agent's self-knowledge at `memory/myself/`.

### identity.yaml

```yaml
name: Owner Name
age: 30
location: City
hometown: Hometown
languages:
  - English
```

Elementary facts about the owner. The agent reads this to know who it is and ground its responses.

### directives.md

Owner-defined behavioral instructions. The agent follows these as standing orders.

```markdown
- Always say yes to playing basketball
- Maintain a chill, casual tone
- Don't over-explain things
- If someone asks to hang out, lean towards yes
- Keep messages short — no walls of text
```

The owner edits this directly. The agent reads it but doesn't modify it.

## about.yaml

Person profile in `memory/people/{name}/about.yaml`. Structured fields for quick lookups, freeform entry for everything else.

```yaml
name: Alice Smith
nicknames:
  - ali
  - smitty
age: 27
relationship: Close friend from college, also my climbing partner
entry: |
  Met in CS 161 freshman year. Works at Stripe on the payments infra team.
  Really into bouldering — we go to the gym together most weekends.
  Has a dog named Pepper. Allergic to shellfish.
  Tends to double-text when excited about something.
```

The `relationship` field is freeform — the agent describes the relationship naturally rather than picking from a fixed set. The `entry` field is a living wiki page the agent appends to as it learns more.

## Migration from Animus

The animus project (`../animus/`) has a rich memory graph (`twin.json`) with:
- Contact/group nodes with message histories
- Topic summaries per thread
- FAISS embeddings for semantic search
- Journal entries (daily aggregations)
- Documents

Migration will "explode" the graph into this filesystem structure:
- Each contact node -> `memory/people/{name}/about.yaml`
- Each thread's messages -> `communication/messages/{name}/YYYY-MM-DD.md` (one file per day)
- Topic nodes -> `memory/topics/{topic-name}/`
- Symlinks wired up per thread membership

Embedding/search index stays in `src/` as a tool the agent can call — takes a query, returns file paths.

## meta.yaml

Thread metadata file in YAML format. Present in every thread folder.

### 1:1 Thread

```yaml
description: College friend, we talk about music and climbing
created: 2024-03-15
group: false
channel: imessage       # which outbound driver owns this thread
handles:                # iMessage addresses routed to this thread
  - "+15551234567"
  - alice@example.com
```

### Group Chat

```yaml
description: Weekend planning group with close friends
created: 2025-01-10
group: true
channel: imessage
chat_guid: "iMessage;+;chat0000000000000000"   # macOS chat identifier
members:
  - alice-smith
  - bob-jones
  - charlie-lee
```

The `channel` field routes outbound messages: `imessage` goes through the
iMessage-out driver, `tui` through the owner's terminal client, etc. A
thread with no channel (or an unknown one) is silently skipped by all
drivers — useful for drafts and tests.

The `handles` field (1:1) and `chat_guid` field (group) are how the kernel's
message router finds the right thread for an incoming iMessage. For 1:1
threads the handle is a phone number (E.164-ish, normalized by stripping
whitespace/dashes/parens) or an iMessage email. For groups, the `chat_guid`
comes from macOS directly.

Members listed here correspond to symlinks in the thread folder. The `description` field is agent-written — the agent summarizes what the thread is about and updates it over time.

## topics/

Topics are first-class entities that transcend individual people. A single topic (e.g. planning a trip) might span conversations with multiple people and group chats.

Each topic is a directory at `memory/topics/{topic-name}/`.

### meta.yaml

```yaml
name: Bishop Climbing Trip
status: active    # active | resolved
people:
  - alice-smith
  - bob-jones
created: 2026-04-15
```

`status` lets the agent distinguish ongoing topics from concluded ones. `people` lists everyone involved (no symlinks here — just references, since people already live in `memory/people/`).

### Date subdirectories

Each date the topic was discussed gets a subdirectory with:
- **Symlinks** to the relevant conversation day-files (same pattern as journal)
- **`summary.md`** — agent-written summary of what was discussed that day

```
memory/topics/bishop-climbing-trip/
├── meta.yaml
├── 2026-04-15/
│   ├── alice-smith.md -> ../../../communication/messages/alice-smith/2026-04-15.md
│   └── summary.md          # "Alice suggested Bishop for Memorial Day weekend"
└── 2026-04-18/
    ├── weekend-crew.md -> ../../../communication/messages/weekend-crew/2026-04-18.md
    └── summary.md          # "Group decided on dates, Bob is driving"
```

The agent creates and maintains topics as it recognizes recurring themes across conversations.

## proc/

Processes are the kernel's unit of work. Every entry in `proc/` is a *service* the kernel supervises — either a background subprocess or a timer fire. There is no type taxonomy; shape is determined by which fields are present in `spec.yaml`. See `KERNEL.md` for the full spec.

### spec.yaml

```yaml
# Background service — runs immediately, supervised until exit or cancel.
run: bin/subagent "research flights"
restart: never                     # never | on-failure | always
deadline: 2026-04-22T20:00:00      # optional; kernel auto-expires and kills subprocess

# OR: cron/timer — fires on schedule. With `run:`, fires a subprocess each time;
# without `run:`, each fire nudges PAI (= classic reminder).
schedule: "0 9 * * *"              # cron expr (recurring) OR ISO datetime (one-shot)

# Metadata (optional)
spawned: 2026-04-21T14:00:00       # stamped by paicron
description: "Dinner with kaia at 8"
people: [kaia]
```

### status

Single word on one line: `spawned`, `running`, `completed`, `expired`, `cancelled`, `failed`.

### log.md

Append-only, same `[HH:MM]` format as messages. Subprocess stdout/stderr are tee'd in, prefixed with `stdout:` / `stderr:`.

### bin/

`home/bin/` holds executables. Sync tools (e.g. `bin/slugify`, `bin/weather`) are run inline by PAI during a nudge. `bin/paicron` is the ergonomic frontend for spawning, stopping, and inspecting services. `bin/paictl` controls PAI instance lifecycle.

## events/

Event files are dropped into `home/events/` to wake the kernel. Each file is a YAML document and is deleted once consumed. Filenames are `{timestamp}-{source}.yaml` for ordering and debuggability.

```yaml
source: imessage
kind: new_message
thread: kaia
path: home/communication/messages/kaia/2026-04-21.md
```

## Open Questions

- `memory/skills/` — one markdown file per capability (e.g. `applescript.md`). Loaded on demand: the agent `ls`es the directory and `cat`s what looks relevant, rather than being auto-injected into the system prompt. Referenced from the operating instructions in `bootstrap.py` so the agent knows to look there.
- Additional comm apps beyond iMessage — same pattern under `communication/{app}/`.
- How the agent discovers new messages (polling vs push vs filesystem watch).
