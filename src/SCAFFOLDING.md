# PAI Scaffolding

## Philosophy

Everything is a filesystem. The agent navigates its world using standard shell primitives (`ls`, `cat`, `grep`, `find`, `tail`, `echo >>`). No custom APIs, no blind graph traversal. Relationships are symlinks. Data is plain text.

## Live Directory Structure

```
live/
├── communication/
│   └── messages/                        # iMessage
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
├── proc/                                # running processes (kernel-managed)
│   └── {process-slug}/
│       ├── spec.yaml                    # process definition
│       ├── status                       # spawned | running | completed | expired | cancelled
│       └── log.md                       # append-only activity log
├── events/                              # kernel inbox — consumed on read
│   └── {timestamp}-{source}.yaml        # one event per file
├── tmp/                                 # ephemeral file storage
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
name: Arda
age: 22
location: San Francisco
hometown: Istanbul
languages:
  - English
  - Turkish
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
```

### Group Chat

```yaml
description: Weekend planning group with close friends
created: 2025-01-10
group: true
members:
  - alice-smith
  - bob-jones
  - charlie-lee
```

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

Processes are the kernel's unit of work — plans, reminders, follow-ups, cron jobs, subagents. Each process is a directory in `live/proc/` with three files. See `src/KERNEL.md` for the full spec.

### spec.yaml

```yaml
type: plan                     # plan | follow-up | reminder | cron | subagent
spawned: 2026-04-21T14:00:00
deadline: 2026-04-22T20:00:00  # one-shot types
schedule: "0 0 * * *"          # cron types
people:
  - kaia
description: Dinner at gyro project tomorrow at 8
resolve_on: deadline           # deadline | confirmation | dependency | completion | schedule
depends_on: null
```

### status

Single word on one line: `spawned`, `running`, `completed`, `expired`, `cancelled`.

### log.md

Append-only, same `[HH:MM]` format as messages.

## events/

Event files are dropped into `live/events/` to wake the kernel. Each file is a YAML document and is deleted once consumed. Filenames are `{timestamp}-{source}.yaml` for ordering and debuggability.

```yaml
source: imessage
kind: new_message
thread: kaia
path: live/communication/messages/kaia/2026-04-21.md
```

## Open Questions

- `memory/skills/` structure — similar concept to Claude Code skills (reusable prompt fragments the agent can invoke). TBD.
- Additional comm apps beyond iMessage — same pattern under `communication/{app}/`.
- How the agent discovers new messages (polling vs push vs filesystem watch).
