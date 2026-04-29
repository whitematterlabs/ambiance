# PAI Filesystem (FHS-aligned)

> **Status: forward-looking spec.** `SCAFFOLDING.md` describes the v1 reality.
> This document describes where we are going. Until migration lands, the two
> diverge — read SCAFFOLDING for what exists today, read this for what we're
> building toward.

## Purpose

PAI's scope is not a chat agent — it is a full computer user, an AI OS. A
self-healing kernel. Self-writing packages. Self-authored drivers. Multiple
jailed PAIs sharing a source of truth, supervised by a privileged kernelPAI.

To make that tractable, the agent's filesystem mirrors the Linux FHS almost
1:1. PAI navigates its world with the same primitives and the same mental
model a sysadmin uses on Linux. No bespoke ontology to learn — the world
*is* a Unix system.

## Design principles

- **Linux semantics are load-bearing.** Borrowing a directory name commits
  us to roughly its Linux meaning. Repurposing `/lib/` to mean "memories"
  or `/root/` to mean "kernel ops" creates confusion that compounds. When
  in doubt, look up what the FHS says, and either honor it or pick a
  different path.
- **kernelPAI = PID 1 = root user.** Exactly one privileged PAI. Lives in
  `/root/`. Owns mutations to `/etc/`, `/sys/`, `/boot/`, `/sbin/`,
  `/usr/sbin/`. Worker PAIs send elevation requests; they do not write
  these paths directly.
- **Worker PAIs are jailed users.** Each lives in `/home/<pai>/`. Their
  view of the world is mostly read-only outside their home; writes go
  to their home, to `/tmp/`, or via elevation requests to kernelPAI.
- **Canonical state is shared; per-PAI state is local.** Ground truth
  (people, topics, journal, messages) lives in `/var/lib/pai/` and
  `/var/spool/`. PAIs read it via symlinks under their home, and
  contribute back through a write path that's still being designed.
- **Code, config, and runtime state are three different things.** A
  driver has source code (`/usr/lib/pai/drivers/<name>/`), config
  (`/etc/drivers/<name>/`), and live state (`/sys/drivers/<name>/`).
  Conflating these is the v1 wart this layout fixes.

## Top-level tree

```
/
├── boot/                          kernel boot artifacts, recovery snapshots
│   └── recovery/                  known-good snapshots for self-heal rollback
│
├── bin/    → /usr/bin             (symlink; usrmerge)
├── sbin/   → /usr/sbin            (symlink; usrmerge)
├── lib/    → /usr/lib             (symlink; usrmerge)
│
├── etc/                           system config
│   ├── config.yaml                kernel + PAI fleet declaration
│   ├── drivers/<name>/events.yaml per-driver event-kind manifests
│   └── pai/<name>.yaml            per-PAI config (identity refs, jail policy)
│
├── dev/                           [DEFERRED] external service endpoints
│
├── home/                          worker PAI homes (jailed)
│   └── <pai>/
│       ├── identity.yaml
│       ├── directives.md
│       ├── memory/
│       │   ├── shared/            → symlinks into /var/lib/pai/memory/
│       │   └── private/           per-PAI writable memory
│       ├── inbox/                 messages addressed to this PAI
│       ├── workspace/             persistent scratch
│       └── tmp/                   per-PAI ephemeral
│
├── root/                          kernelPAI's home (PID 1)
│   ├── identity.yaml
│   ├── directives.md
│   └── inbox/                     elevation requests / nudges from workers
│
├── opt/                           installed packages (package manager target)
│   └── <pkg>/                     self-contained: bin/, lib/, share/
│
├── proc/                          per-PAI runtime process state
│   └── <pai>/<service-slug>/      spec.yaml, status, log.md (see KERNEL.md)
│
├── run/                           runtime state since boot (cleared at boot)
│   ├── locks/
│   ├── sockets/
│   └── pids/
│
├── sys/                           live driver + kernel runtime state
│   ├── drivers/<name>/            current poller status, queue depth, last event
│   └── kernel/                    kernel manager liveness, reload state
│
├── tmp/                           ephemeral, cleared on boot
│
├── usr/                           secondary hierarchy
│   ├── bin/                       PAI-callable CLI tools (e.g. paictl)
│   ├── sbin/                      kernel-only tools (self-heal, fleet ops)
│   ├── lib/
│   │   └── pai/
│   │       ├── drivers/<name>/    driver source/runtime code
│   │       └── skills/<name>/     skill source code
│   ├── share/
│   │   └── pai/                   read-only factory defaults (seed data)
│   │       ├── memory-seed/       baseline identity/directives templates
│   │       └── skills-base/       shipped skill library
│   └── src/
│       └── pai/                   Python source (current src/)
│
└── var/                           persistent mutable state
    ├── lib/
    │   └── pai/
    │       ├── memory/            canonical ground truth
    │       │   ├── people/<name>/
    │       │   ├── topics/<topic>/
    │       │   └── journal/<date>/
    │       └── packages/          installed-package state metadata
    ├── log/
    │   ├── kernel/
    │   ├── drivers/<name>/
    │   └── pai/<pai>/
    ├── spool/
    │   └── communication/         append-only message logs
    │       └── messages/<contact-or-group>/<YYYY-MM-DD>.md
    └── cache/
```

## Per-directory semantics

### `/boot/`
Kernel boot artifacts: bootloader-equivalent configs, recovery snapshots,
boot logs. The self-heal story lives here — `boot/recovery/` holds known-good
snapshots that kernelPAI can roll back to if a self-mutation breaks the
system.

### `/bin/`, `/sbin/`, `/lib/` → `/usr/bin/`, `/usr/sbin/`, `/usr/lib/`
Modern Linux usrmerge: these top-level dirs are symlinks into `/usr/`. We
follow the same convention. Don't put files directly in `/bin/` or `/lib/` —
put them in `/usr/bin/` and `/usr/lib/`. The top-level symlinks exist so
`#!/bin/sh` and similar muscle memory still works.

### `/etc/`
System config. Already exists today. Owned by kernelPAI; worker PAIs read
but do not write. `etc/config.yaml` is the source of truth for the PAI
fleet declaration (reconciled into `/proc/` at boot and on
`kernel:reload_config`). `etc/drivers/<name>/events.yaml` enumerates each
driver's event-kinds — the source of truth for `wake_on:` patterns.

### `/dev/`
**Deferred.** External services (Gmail, Telegram, Calendar) are device-like
I/O endpoints with read/write semantics — `cat /dev/gmail` to see new mail,
`echo ... > /dev/telegram` to send. Whether to model them this way, or
keep them purely behind drivers + events, is an open question. The slot is
reserved.

### `/home/<pai>/`
Each worker PAI gets its own home directory. Layout:

- `identity.yaml`, `directives.md` — who the PAI is, how it behaves
- `memory/shared/` — symlinks into `/var/lib/pai/memory/` for read-through
  access to canonical ground truth
- `memory/private/` — writable, per-PAI memory the PAI owns outright
- `inbox/` — messages and nudges addressed specifically to this PAI
- `workspace/` — persistent scratch
- `tmp/` — per-PAI ephemeral, separate from system `/tmp/`

Worker PAIs are jailed: their writes outside their home are rejected by
default. They reach the rest of the system through reads, through their
inbox, and through elevation requests to kernelPAI.

### `/root/`
kernelPAI's home. Same shape as `/home/<pai>/` but privileged. The `inbox/`
here is where worker PAIs drop elevation requests ("please add this to
`/etc/drivers/`", "please install this package to `/opt/`"). kernelPAI is
the only PAI permitted to mutate `/etc/`, `/sys/`, `/boot/`, `/sbin/`,
`/usr/sbin/`, `/var/lib/pai/packages/`.

### `/opt/`
Target for the package manager. Each installed package is self-contained
under `/opt/<pkg>/` with its own `bin/`, `lib/`, `share/`. Package state
metadata (what's installed, version, install date) lives in
`/var/lib/pai/packages/`. The package manager itself is a kernelPAI tool
under `/usr/sbin/`.

### `/proc/`
Per-PAI runtime process state. Already exists today as `home/proc/`. Each
running service is a directory with `spec.yaml`, `status`, `log.md` (see
`KERNEL.md`). Under multi-PAI, this becomes `/proc/<pai>/<service-slug>/`.

### `/run/`
Runtime state created since boot: lockfiles, Unix sockets, PID files.
Cleared at boot. Use this for transient coordination state — not for
anything the system needs to remember across restarts.

### `/sys/`
Live driver and kernel runtime state. This is the sysfs analogue: a
read-mostly window into what's running *right now*. Per-driver:
`/sys/drivers/<name>/` exposes current poller status, queue depth, last
event timestamp. `/sys/kernel/` exposes kernel manager liveness and
reload state. Drivers' *code* lives in `/usr/lib/pai/drivers/`, not here.

### `/tmp/`
System-wide ephemeral. Cleared on boot. Per-PAI ephemerals belong in
`/home/<pai>/tmp/` so they don't leak across PAIs.

### `/usr/`
The secondary hierarchy: most of the OS lives here.

- `/usr/bin/` — executables PAIs can call (e.g. `paictl`)
- `/usr/sbin/` — kernel-only executables (self-heal scripts, fleet ops,
  package manager)
- `/usr/lib/pai/drivers/<name>/` — driver source code
- `/usr/lib/pai/skills/<name>/` — skill source code
- `/usr/share/pai/` — read-only factory defaults (seed identity templates,
  base skill library shipped with the install). On first boot, kernelPAI
  copies relevant seed data into `/var/lib/pai/`. Same pattern Debian uses
  for `/usr/share/` → `/var/lib/`.
- `/usr/src/pai/` — Python source code (the current `src/`)

### `/var/`
All persistent mutable state.

- `/var/lib/pai/memory/` — canonical ground truth (see Memory Layout below)
- `/var/lib/pai/packages/` — installed-package state metadata
- `/var/log/{kernel,drivers/<name>,pai/<pai>}/` — append-only logs
- `/var/spool/communication/` — message queues (see Communication Layout)
- `/var/cache/` — regenerable derived state (embeddings, indexes, etc.)

## Memory layout

Canonical memory lives in `/var/lib/pai/memory/`. This is the source of
truth — there is one record of who Alice is, one record of the Bishop
trip topic, one journal entry per day.

```
/var/lib/pai/memory/
├── people/
│   └── <name>/
│       └── about.yaml
├── topics/
│   └── <topic>/
│       ├── meta.yaml
│       └── <YYYY-MM-DD>/
│           ├── <thread>.md -> /var/spool/communication/messages/<thread>/<YYYY-MM-DD>.md
│           └── summary.md
└── journal/
    └── <YYYY-MM-DD>/
        ├── <thread>.md -> /var/spool/communication/messages/<thread>/<YYYY-MM-DD>.md
        └── notes.md
```

Each PAI sees this through symlinks under its own home:

```
/home/<pai>/memory/
├── shared/
│   ├── people    -> /var/lib/pai/memory/people
│   ├── topics    -> /var/lib/pai/memory/topics
│   └── journal   -> /var/lib/pai/memory/journal
└── private/
    └── ...                 # whatever this PAI wants to remember alone
```

`shared/` gives the PAI read-through access to ground truth using ordinary
shell tools — `cat ~/memory/shared/people/alice/about.yaml` works the same
way `cat home/memory/people/alice/about.yaml` works today. `private/` is
the PAI's own writable scratch.

The write path through `shared/` (does it mutate ground truth directly?
does it copy-on-write to `private/`? does it route through kernelPAI?) is
explicitly open — see Open Questions.

## Driver layout

Each driver has three locations, each with a different Linux meaning:

| Path | Role | Owner |
|---|---|---|
| `/etc/drivers/<name>/events.yaml` | Config (event manifest, settings) | kernelPAI writes |
| `/usr/lib/pai/drivers/<name>/` | Source code | kernelPAI writes |
| `/sys/drivers/<name>/` | Live runtime state (poller status, cursors, last event) | kernel process writes |

This separation is what `/sys/` is for in real Linux too: a window into
what the kernel and its drivers are doing right now, distinct from the
code that defines them and the config that parameterizes them.

## Communication layout

Messages move out of any PAI's home and into shared spool — Linux's
`/var/spool/` is exactly the slot for mail and message queues. Multiple
PAIs may converse with the same contact, so messages aren't private to
one PAI.

```
/var/spool/communication/messages/
├── <contact-name>/
│   ├── meta.yaml
│   ├── 2026-04-18.md
│   └── 2026-04-19.md
└── <group-name>/
    ├── meta.yaml
    └── 2026-04-18.md
```

Per-PAI inboxes symlink the relevant threads in:

```
/home/<pai>/inbox/
├── alice-smith -> /var/spool/communication/messages/alice-smith/
└── weekend-crew -> /var/spool/communication/messages/weekend-crew/
```

A PAI on email duty doesn't see the iMessage threads. A PAI handling
Alice doesn't see the group chat unless explicitly subscribed. The
filesystem expresses access control through which symlinks exist.

## kernelPAI vs worker PAIs

| Path | kernelPAI | worker PAI |
|---|---|---|
| `/etc/`, `/sys/`, `/boot/`, `/sbin/`, `/usr/sbin/` | read + write | read only |
| `/usr/lib/`, `/usr/share/`, `/usr/src/`, `/opt/` | read + write (via package mgr) | read only |
| `/var/lib/pai/memory/` | read + write | read; write semantics TBD |
| `/var/spool/communication/` | read + write | read + write (their own threads) |
| `/var/log/` | read + write | append to own log; read all |
| `/root/` | full | none |
| `/home/<own>/` | n/a | full |
| `/home/<other>/` | full | none |
| `/tmp/`, `/run/`, `/proc/`, `/dev/` | full | scoped to own processes |

Worker PAIs request elevation by dropping a request file into
`/root/inbox/`. kernelPAI processes the inbox like any other event source
(see `KERNEL.md`).

## Deltas from `SCAFFOLDING.md`

This layout changes several things relative to v1:

- **`home/` → `/home/<pai>/`.** Single PAI home becomes per-PAI homes
  under a top-level `/home/`. Each home is a jailed sandbox.
- **`home/memory/` → `/var/lib/pai/memory/` + `/home/<pai>/memory/`.**
  Canonical memory moves out of any one PAI's home into shared
  `/var/lib/pai/`. Per-PAI homes get a `memory/shared/` (symlinks in)
  and `memory/private/` (own writable).
- **`home/communication/` → `/var/spool/communication/`.** Messages move
  to shared spool, since multiple PAIs may converse with the same
  contact.
- **`home/proc/` → `/proc/<pai>/`.** Process state namespaced by PAI.
- **`home/events/` → routed to `/home/<pai>/inbox/` or `/root/inbox/`.**
  Events get addressed to a specific PAI rather than dropped into a
  global inbox.
- **`home/bin/` → `/usr/bin/` (PAI-callable) and `/usr/sbin/` (kernel-only).**
  Split by privilege. `paictl` lives in `/usr/bin/`; self-heal and
  package manager live in `/usr/sbin/`.
- **`home/tmp/drivers/` → `/sys/drivers/<name>/`.** Driver runtime state
  (cursors, last event) moves to sysfs, where Linux puts live driver
  state.
- **`packages/` → `/opt/`.** Reusable bundles install into `/opt/`,
  Linux's slot for self-contained third-party packages. State metadata
  lives in `/var/lib/pai/packages/`.
- **`src/` → `/usr/src/pai/`.** Python source moves to its FHS slot. The
  exact mechanism (symlink vs. install-time copy) is an open question.
- **kernelPAI gets `/root/`.** The privileged PAI is exactly one user,
  with its own home, separate from worker PAIs.
- **`/etc/` already in the right slot** — no move needed.

The `etc/config.yaml`, `etc/drivers/{driver}/events.yaml`, and `proc/`
shapes documented in `SCAFFOLDING.md` and `KERNEL.md` remain valid; only
their location changes.

## Open Questions

- **FHS root location relative to the dev repo.** Three options:
  A) repo root *is* `/` (developer scaffolding like `pyproject.toml`,
  `tests/`, `.venv` lives at top of `/`); B) repo root contains a
  subdirectory (e.g. `root/`) that *is* `/`, keeping dev scaffolding
  separate from the agent's perceived world; C) repo root is `/` but
  developer scaffolding is symlink-aliased into `/usr/src/pai/`.
- **`src/` dissipation strategy.** Whether Python source stays as a
  symlink at `/usr/src/pai/` (visible/editable to PAI in place) or
  gets installed/copied there at boot (separating "what humans edit"
  from "what PAI sees").
- **`/dev/` design.** Whether external services (Gmail, Telegram,
  Calendar) get modeled as device-like endpoints under `/dev/`, or
  stay purely behind drivers + events.
- **Multi-PAI memory sharing semantics.** What scopes a memory as
  shared vs. private — topic/sensitivity-based (information-shaped),
  PAI-based (role-shaped), or hybrid. Deferred per YAGNI.
- **Write semantics through shared symlinks.** When a PAI writes
  through `~/memory/shared/`, does it mutate ground truth directly,
  copy-on-write into `~/memory/private/`, or route through kernelPAI
  for promotion? The overlayfs analogue is appealing but adds
  machinery.
