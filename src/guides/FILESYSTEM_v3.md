# PAI Filesystem v3

> **Status: forward-looking spec.** Supersedes `FILESYSTEM_v2.md`,
> `FILESYSTEM.md` (v1 FHS design), and `SCAFFOLDING.md` (v0 layout).
> Read this for where we're going.

## What changed from v2

- **`/boot/` holds kernel source code; `/sbin/init` is the entrypoint.**
  The supervisor (the "kernel") is real Python code that runs as PID 1.
  `/sbin/init` is a thin shim that `exec`s into the kernel — init
  *becomes* the kernel, the way Linux's `/sbin/init` *is* systemd.
  "Boot" is just the kernel's first-phase startup, not a separate
  process. Recovery snapshots remain a deferred `/boot/recovery/` slot.
- **kernelPAI is supervised, not the supervisor.** kernelPAI is a
  privileged *agent* that runs as `/proc/<pid>/` like any other PAI,
  with its home at `/root/`. It mediates kernel state by sending
  events (`kernel:reload_config`, etc.); it does not run the supervise
  loop itself. The kernel (pure code, no LLM) is PID 1; kernelPAI is
  one of its supervised children. A crash in kernelPAI's reasoning
  loop does not take the system down.
- **Two-layer process view: `/proc/<pid>/` + `/run/pais/<name>/`.**
  `/proc/` is PID-keyed and holds every running PAI process — declared,
  peer, or transient subagent — matching Linux semantics. `/run/pais/`
  is name-keyed and holds only declared/long-lived PAIs, with a
  symlink to the current `/proc/<pid>/`, an `inbox/`, and a `pid` file.
  Subagents get `/proc/` entries but no `/run/pais/` entry — making
  "addressable by name" an explicit privilege granted by `paiadd`.
- **Three-tool split.** `paiman` (bundles) / `paiadd` (instances) /
  `paictl` (processes). Each tool owns one noun, one layer, one path.
  No flags that cross layers.
- **Bundle / instance / process as three explicit layers.** A bundle is
  a template. Adding a bundle produces an instance. Starting an instance
  produces a process. Each transition is one tool.
- **One process per PAI.** kernelPAI supervises *the PAI*, not its
  internal services. Drivers, subagents, sub-tasks are children of the
  PAI process — the PAI supervises its own tree. `/proc/<pai>/` is the
  unit; there is no `/proc/<pai>/<svc>/`.
- **Mountable bundles.** Bundles are portable, downloadable artifacts.
  `paiman install` puts them in `/opt/<pkg>/<ver>/`; `paiadd` stitches
  them into `/home/<pai>/` over instance state at `/var/lib/instances/<pai>/`.
  Bundle is immutable; instance state is sacred.
- **Dev path bypasses `/opt/`.** Bundle source authored in place at
  `/usr/lib/pais/<name>/`. `paiadd` stitches directly from there. `paiman`
  is only involved for built/release artifacts.
- **`/mnt/` reserved.** Guest/trial PAIs (mounted, not added) get `/mnt/`
  when the jailing story lights up. Until then, all PAIs are household
  members in `/home/`.

Carried forward from v2:

- FHS as convention, not as kernel — PAI runs on macOS host, not in a
  Linux container/VM. Going there would sever native macOS surfaces.
- Privilege and jailing are convention, not enforcement — deferred until
  multi-PAI demands real plumbing.
- No `/pai/` nesting; no usrmerge symlinks.

## Distribution vs. installed system

Two distinct artifacts live at two distinct paths, and it's worth being
clear which is which:

- **The repo** (this codebase) is a **Python package + git repo**. It's
  what humans edit, what CI builds against, what gets published. Its
  layout is conventional Python (`pyproject.toml`, `src/`, `tests/`,
  etc.) — *not* FHS-shaped.
- **The installed system** is an actual quasi-Linux filesystem rooted at
  `/`. Install lays the repo's contents into FHS slots: Python source
  goes to `/usr/src/`, baseline skills/drivers go to `/usr/lib/`,
  shipped prompts go to `/usr/share/prompts/`, the three CLI tools go
  to `/bin/`, and so on.

Everything in this document describes the **installed system**, not the
repo. The repo is the build input; `/` is the runtime.

How install actually works (symlink the repo's `src/` into `/usr/src/`?
copy at install time? bind-mount?) is an implementation detail tracked
in Open Questions.

### Today's `src/` → FHS slots

| Source (today) | Destination | Notes |
|---|---|---|
| `src/pai.py` | `~/.pai/sbin/init` | Refactored into the kernel entrypoint |
| `src/kernel/` | `~/.pai/boot/` | Supervisor "image" — the running kernel |
| `src/drivers/<name>/` | split three ways | `events.yaml` → `/etc/drivers/<name>/`, code → `/usr/lib/drivers/<name>/`, runtime → `/sys/drivers/<name>/` |
| `src/bin/` | `~/.pai/usr/bin/` (or `/bin/`) | PAI-callable tools |
| `src/tui/` | `~/.pai/sbin/` | Owner's terminal client (privileged ops) |
| `src/migrate.py` | `~/.pai/sbin/` | One-shot kernelPAI op |
| `src/reset.py` | `~/.pai/sbin/` | One-shot kernelPAI op |
| `src/prompts/` | `~/.pai/usr/share/prompts/` | Shipped baseline prompts |
| `src/guides/` | `~/.pai/usr/share/doc/` | Shipped documentation |
| `src/seed/` | *removed* | Folded into bundle `defaults/` |

## Mental model: bundle → instance → process

```
  bundle  ──paiadd──▶  instance  ──paictl start──▶  process
 (paiman                                              (paictl
 installs)                                          start/stop)
```

| Layer | Noun | What it is | Where it lives |
|---|---|---|---|
| **Bundle** | template | manifest + defaults + bundled drivers/skills | `/opt/<pkg>/<ver>/` (release) or `/usr/lib/pais/<name>/` (dev) |
| **Instance** | identity | a configured PAI: name, identity, private memory, workspace | `/home/<pai>/` (stitched view) + `/var/lib/instances/<pai>/` (real state) |
| **Process** | runtime | the running PAI and its child tree (drivers, subagents) | `/proc/<pai>/` |

A bundle can be instantiated multiple times (same `weather-pai` bundle,
two instances with different names). An instance can be started and
stopped many times across its life. A process exists only while the
instance is running.

## The three tools

```
/bin/paiman   ── bundles    (apt   analogue)
/bin/paiadd   ── instances  (useradd analogue)   /bin/paidel
/bin/paictl   ── processes  (systemctl analogue)
```

| Tool | Operates on | Verbs |
|---|---|---|
| `paiman` | `/opt/<pkg>/<ver>/` | `init`, `install`, `uninstall`, `upgrade`, `list` |
| `paiadd` / `paidel` | `/home/<pai>/`, `/var/lib/instances/<pai>/`, `/etc/config.yaml` | `paiadd <name>`, `paidel <name> [--purge]`, `paiadd list` |
| `paictl` | `/proc/<pai>/` | `start`, `stop`, `restart`, `status`, `logs` |

`paiman` doesn't know what an instance is. `paiadd` doesn't know what a
process is. `paictl` doesn't know what a bundle is. Crossing layers
means composing tools, not adding flags.

`paictl` operates at PAI granularity only. Want to restart a driver
inside email-pai? Restart email-pai, or send it a message asking it
to recycle the driver. Internal supervision is the PAI's job, not
kernelPAI's.

## Top-level tree

```
/
├── boot/                  kernel source code (the supervisor "image")
│   └── recovery/          snapshots before kernelPAI mutations (deferred)
├── bin/                   PAI-callable tools (paiman, paiadd, paidel, paictl, …)
├── sbin/                  kernelPAI-only tools, plus init
│   └── init               entrypoint — execs into the kernel as PID 1
├── etc/                   config (read by all, written by kernelPAI)
│   ├── config.yaml        fleet declaration: list of PAIs
│   ├── drivers/<name>/events.yaml
│   └── prompts/           per-install prompt overrides
├── home/<pai>/            per-PAI workspace — stitched symlink tree
│   ├── identity.yaml      → /var/lib/instances/<pai>/identity.yaml
│   ├── directives.md      → /var/lib/instances/<pai>/directives.md
│   ├── prompts/           → /var/lib/instances/<pai>/prompts/
│   ├── memory/
│   │   ├── shared         → /var/lib/memory/
│   │   └── private        → /var/lib/instances/<pai>/memory/private/
│   ├── inbox              → /var/lib/instances/<pai>/inbox/
│   ├── workspace          → /var/lib/instances/<pai>/workspace/
│   └── tmp/               real dir, ephemeral
├── root/                  kernelPAI's home (same shape as /home/<pai>/)
├── mnt/                   guest/trial PAIs (deferred — pairs with jailing)
├── opt/<pkg>/<ver>/       installed bundles (immutable, paiman target)
├── proc/<pid>/            running PAI processes (PID-keyed; status, cmdline, fd/, log)
├── run/pais/<name>/       declared/long-lived PAIs (name-keyed; → /proc/<pid>/, inbox/, pid)
├── sys/drivers/<name>/    live driver runtime state (cursors, last event)
├── tmp/                   system-wide ephemeral, cleared on boot
├── usr/
│   ├── lib/
│   │   ├── drivers/<name>/      driver source code
│   │   ├── skills/<name>/       skill source code
│   │   ├── pais/<name>/         in-development PAI bundle source
│   │   └── venv/                Python virtualenv (uv-managed)
│   ├── share/prompts/           shipped baseline prompts
│   └── src/                     Python source (kernelPAI, libraries)
└── var/
    ├── lib/
    │   ├── memory/              canonical ground truth (multi-PAI shared)
    │   │   ├── people/<name>/about.yaml
    │   │   ├── topics/<topic>/
    │   │   └── journal/<date>/
    │   ├── instances/<pai>/     mutable per-PAI instance state
    │   └── packages/            paiman state metadata (deferred)
    ├── log/{kernel,drivers/<name>,pai/<pai>}/
    ├── spool/communication/messages/<thread>/<date>.md
    └── cache/                   regenerable derived state (deferred)
```

## Per-directory semantics

### `/boot/`
Kernel source code — the supervisor "image." Loaded by `/sbin/init`
at startup. Pure Python, no LLM: reconciles `/etc/config.yaml` into
`/run/pais/`, spawns and reaps PAI processes, routes events,
handles signals. This is what runs as PID 1.

`/boot/recovery/` (deferred) holds snapshots of `/etc/` taken before
kernelPAI mutates it, for rollback on failed reload. Hot-swappable
kernel modules also deferred.

### `/bin/` and `/sbin/`
Privilege-split binaries. `/bin/` = any PAI may call. `/sbin/` =
kernelPAI-only (self-heal, fleet ops) plus the system entrypoint.
The three core PAI tools (`paiman`, `paiadd`, `paidel`, `paictl`)
all live in `/bin/`. No usrmerge.

`/sbin/init` is the entrypoint. It's a thin shim that verifies
`~/.pai/` layout, then `exec`s into the kernel from `/boot/`. After
exec, PID 1 *is* the kernel — there is no separate init process
hanging around. Boot phases (driver probe, fleet reconcile,
kernelPAI start, fleet start) are the kernel's first-phase
startup, not a pre-handoff script.

Boot sequence:

1. **Sanity check** — verify required dirs exist (`etc/`, `var/lib/`,
   `proc/`, `run/`); bail loudly if not.
2. **Clean ephemeral state** — wipe `/tmp/`, `/run/pais/`, stale
   `/proc/<pid>/` dirs from prior boots.
3. **Driver probe** — for each driver in `/etc/drivers/`, run a
   `health()` check (paths exist, deps importable, credentials
   present). Log to `/var/log/kernel/boot.log`.
4. **Reconcile fleet** — read `/etc/config.yaml`, populate
   `/run/pais/<name>/` for each declared PAI (registered, not started).
5. **Start kernelPAI first** — privileged agent must be up before
   peers can escalate to it.
6. **Start fleet** — spawn each remaining PAI per its restart policy.
7. **Enter supervise loop** — watch `/proc/`, reap dead processes,
   route events from `/var/spool/events/`, handle `kernel:reload_config`.

### `/etc/`
System config. Read by all, written by kernelPAI (by convention).

- `etc/config.yaml` — fleet declaration. A list of PAIs:
  `{ name, bundle, version, source }`. No `services:` array per PAI —
  internal supervision is the PAI's concern. Reconciled into `/proc/`
  at boot and on `kernel:reload_config`.
- `etc/drivers/<name>/events.yaml` — per-driver event-kind manifest.
- `etc/prompts/` — per-install overrides on top of `/usr/share/prompts/`.

### `/home/<pai>/`
A PAI's workspace. **Built fresh by `paiadd` as a directory of symlinks.**
Nothing of substance lives here directly — every entry points either
into the canonical shared state under `/var/lib/memory/` or into the
PAI's instance state under `/var/lib/instances/<pai>/`. This is what
makes a PAI portable: the home is a view, not a container.

The PAI's process runs with `/home/<pai>/` as its CWD; from inside, it
sees a normal Unix home directory.

Jailing is deferred — for now, "PAIs only write under their home" is
convention.

### `/root/`
kernelPAI's home. Same shape and stitching as `/home/<pai>/`. KernelPAI
is special only by convention: it handles kernel-level work and is the
sole writer of `/etc/`, `/usr/`, `/opt/`, `/var/lib/memory/` (eventually
enforced; today, etiquette).

### `/mnt/<pai>/` *(deferred)*
Mountpoint for guest/trial PAIs. Stateless or scoped state, possibly
read-only access to shared memory. Pairs with the jailing story; lights
up when isolation is real. Until then, every PAI is a household member
in `/home/`. Reserved here so the language stays honest: "mount" means
`/mnt/`, never `/home/`.

### `/opt/<pkg>/<ver>/`
`paiman`'s install target. Immutable, versioned bundles. Multiple
versions of one bundle may coexist (rollback, gradual migration). `paiadd`
stitches `/home/<pai>/` against a chosen version; upgrades replace the
version in-place under `/opt/` and re-point the stitching.

### `/proc/<pid>/`
PID-keyed view of every running PAI process — declared peers,
kernelPAI, and transient subagents alike. Mirrors Linux `/proc/`:
`status`, `cmdline`, `fd/`, current-session log. Created when a
process spawns, removed when it exits.

A PAI process is one OS process; its drivers, subagents, and
sub-tasks are children in its process tree. The kernel supervises
the root; the PAI supervises its tree.

If you want to introspect the tree, that's `ps` territory — and
`ps` for PAIs is just a walk of `/proc/<pid>/` reading `cmdline`
and `status`.

### `/run/pais/<name>/`
Name-keyed view of declared/long-lived PAIs. One directory per
PAI in `/etc/config.yaml`, created by `paiadd` and reconciled at
boot. Contains:

- `current → /proc/<pid>/` — symlink to the live process dir
- `pid` — current PID as a plain file (`kill $(cat pid)` works)
- `inbox/` — name-addressed message drop for IPC
- `spec.yaml` — what should be running (declared state)
- `status` — `running | stopped | failed`
- `log.md` — durable, restart-spanning activity log

Subagents and other transient processes get `/proc/<pid>/` entries
but no `/run/pais/<name>/` entry — making "addressable by name" an
explicit privilege granted at `paiadd` time, not something arbitrary
spawns can claim. Name validation/sanitization happens once, in
`paiadd`.

### `/sys/drivers/<name>/`
Live driver runtime state — sysfs analogue. Cursors, last event,
queue depth. Read-mostly window into "what's running right now."
Distinct from driver code (`/usr/lib/drivers/`) and config (`/etc/drivers/`).

Unchanged from v2.

### `/tmp/`
System-wide ephemeral, cleared on boot. Per-PAI ephemerals belong in
`/home/<pai>/tmp/`.

### `/usr/`
Code, libraries, shipped data.

- `usr/lib/drivers/<name>/` — driver source code.
- `usr/lib/skills/<name>/` — skill source code.
- `usr/lib/pais/<name>/` — **in-development PAI bundle source.**
  `paiadd` stitches directly from here for the dev path. `/opt/` is
  bypassed entirely; the source tree IS the bundle, edited in place.
- `usr/lib/venv/` — Python virtualenv.
- `usr/share/prompts/` — shipped baseline prompts.
- `usr/share/doc/` — shipped documentation (architecture guides,
  filesystem spec, etc.). Where `src/guides/` lands at install time.
- `usr/src/` — Python source (libraries shared across kernel +
  PAIs). The kernel itself lives at `/boot/`, not here.

### `/var/`
All persistent mutable state.

- `var/lib/memory/` — canonical ground truth (see Memory Layout).
- `var/lib/instances/<pai>/` — **per-PAI mutable instance state.** This
  is what survives uninstall/reinstall. Sacred.
- `var/lib/packages/` — paiman metadata (deferred).
- `var/log/` — append-only logs.
- `var/spool/communication/` — message queues (see Communication Layout).
- `var/cache/` — regenerable derived state (deferred).

## Bundle anatomy

A bundle is the template a PAI is instantiated from.

```
/opt/<pkg>/<version>/                     (release; from paiman install)
/usr/lib/pais/<name>/                     (dev; authored in place)
├── manifest.yaml      what this PAI declares it needs and provides
└── defaults/          template files seeded into instance on paiadd
    ├── identity.yaml
    ├── directives.md
    └── prompts/
```

The manifest declares:

- bundle name, version, description
- required drivers (by name, with version constraints)
- required skills (same)
- requested capabilities (which paths it needs read/write — informational
  until a capability system enforces them)
- default instance name (overridable at `paiadd` time)

**Drivers and skills are system-shared dependencies, not bundle-vendored.**
A bundle declares what it needs; `paiman` resolves and installs the
required drivers into `/usr/lib/drivers/<name>/` and skills into
`/usr/lib/skills/<name>/` if they're not already there. Two PAIs that
both need the `gmail` driver share one installed copy. Version pinning
in the manifest handles ABI drift; the system can hold multiple installed
versions of a skill/driver if bundles disagree.

This means a bundle is small — it's mostly identity, directives, prompts,
and a manifest. The heavy code (drivers, skills) is shared infrastructure
managed by `paiman` at the system layer, not duplicated per-bundle.

Bundle content is **immutable** post-install. Edits go to instance state.

## Instance anatomy

An instance is a configured PAI: a name, an identity, private memory,
accumulated state.

```
/var/lib/instances/<pai>/
├── .meta.yaml         { bundle, version, source, added_at }
├── identity.yaml      seeded from defaults; user/PAI may edit
├── directives.md      seeded from defaults
├── prompts/           seeded from defaults
├── memory/private/    PAI's own writable memory
├── workspace/         persistent scratch
└── inbox/             events addressed to this PAI
```

`/home/<pai>/` is a directory of symlinks pointing into here (for private
state) and into `/var/lib/memory/` (for shared state).

**Instance state is sacred:**

- `paidel <name>` removes the fleet entry, drops the `/home/` stitching,
  but leaves `/var/lib/instances/<name>/` intact. Re-adding restores
  the PAI with all memory/workspace.
- `paidel <name> --purge` is the destructive variant.
- `paiman uninstall <bundle>` refuses if any instance references the
  bundle.
- Bundle upgrades use a three-way diff against `defaults/` (rpm/dpkg
  `.rpmnew` pattern): unchanged seeds get auto-updated; diverged seeds
  get a `.new` sibling for manual merge.

## Process supervision

KernelPAI supervises *the PAI*, not what's inside it.

```
  kernelPAI
    └── /proc/email-pai/ (pid 4821)
            ├── driver: gmail-poller    (child)
            ├── driver: nudge-watcher   (child)
            ├── subagent: triage-worker (child, transient)
            └── subagent: drafter       (child, transient)
```

- KernelPAI manages: PAI lifecycle (start, stop, restart), restart
  policy on PAI process exit, top-level liveness.
- The PAI manages: its own drivers, subagents, restart-on-crash for its
  children, internal logging, internal scheduling.

A driver crash inside email-pai is email-pai's problem. It can restart
the driver, log, give up, or escalate via `/root/inbox/`. KernelPAI
only intervenes when the PAI process itself dies.

This means **every PAI inherits internal supervision capability** —
worth pulling into a baseline library every bundle imports (likely a
skill at `/usr/lib/skills/supervisor/`) so bundles don't reimplement it.

## Memory layout

Canonical memory at `/var/lib/memory/`. One record of who Alice is, one
record of the Bishop trip, one journal entry per day. Multi-PAI shared.

```
/var/lib/memory/
├── people/<name>/about.yaml
├── topics/<topic>/
│   ├── meta.yaml
│   └── <YYYY-MM-DD>/
│       ├── <thread>.md → /var/spool/communication/messages/<thread>/<YYYY-MM-DD>.md
│       └── summary.md
└── journal/<YYYY-MM-DD>/
    ├── <thread>.md → /var/spool/communication/messages/<thread>/<YYYY-MM-DD>.md
    └── notes.md
```

PAIs see this through `/home/<pai>/memory/shared/ → /var/lib/memory/`.
Writes through the symlink mutate ground truth — no overlay, no
copy-on-write. When privileged-write enforcement lands, denial happens
at policy, not at filesystem.

`/home/<pai>/memory/private/` (→ `/var/lib/instances/<pai>/memory/private/`)
is the PAI's own writable space.

## Communication layout

Messages live in shared spool. Multiple PAIs may converse with the
same contact, so messages aren't private to one PAI.

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

Format unchanged: `[HH:MM] sender: message text`, one line per message,
one file per day, append-only.

Per-PAI inboxes symlink subscribed threads:

```
/home/<pai>/inbox/  (→ /var/lib/instances/<pai>/inbox/)
├── alice-smith → /var/spool/communication/messages/alice-smith/
└── weekend-crew → /var/spool/communication/messages/weekend-crew/
```

Which symlinks exist controls which threads a PAI sees.

## Driver layout

| Path | Role |
|---|---|
| `/etc/drivers/<name>/events.yaml` | Config (event manifest, settings) |
| `/usr/lib/drivers/<name>/` | Source code |
| `/sys/drivers/<name>/` | Live runtime state |

Linux's three-way split between code, config, and runtime.

## Prompt resolution

When PAI loads a prompt, it walks in order:

1. `/home/<pai>/prompts/<name>` — per-PAI customization
2. `/etc/prompts/<name>` — per-install override
3. `/usr/share/prompts/<name>` — shipped baseline

First hit wins. Same pattern as Debian's `/etc/` shadowing `/usr/share/`.

## Workflows

### Local-dev: kernelPAI scaffolds a new PAI

```bash
# Scaffold the bundle source
paiman init email-pai
# Creates /usr/lib/pais/email-pai/ with manifest stub + defaults stubs

# kernelPAI authors the bundle:
#   /usr/lib/pais/email-pai/manifest.yaml      (deps, capabilities)
#   /usr/lib/pais/email-pai/defaults/identity.yaml
#   /usr/lib/pais/email-pai/defaults/directives.md
#   /usr/lib/pais/email-pai/defaults/prompts/

# Resolve declared deps (drivers, skills); error or scaffold if missing.

# Add to fleet
paiadd email-pai
# - allocates /var/lib/instances/email-pai/
# - copies defaults/* into the instance
# - builds /home/email-pai/ symlink tree
# - appends fleet entry to /etc/config.yaml
# - emits kernel:reload_config

# Start it
paictl start email-pai

# Verify
paictl status email-pai
paictl logs email-pai
```

### Release: install a published bundle

```bash
paiman install weather-pai          # → /opt/weather-pai/1.2.0/
paiadd weather-pai                  # → /home/weather-pai/, /var/lib/instances/weather-pai/
paictl start weather-pai
```

### Teardown

```bash
paictl stop email-pai               # kills the PAI process tree; instance + bundle untouched
paidel email-pai                    # removes fleet entry + /home/ symlinks; instance preserved
paidel email-pai --purge            # also deletes /var/lib/instances/email-pai/
paiman uninstall weather-pai        # removes /opt/weather-pai/; refuses if instances reference it
```

### Upgrade

```bash
paiman install weather-pai@1.3.0    # installs alongside 1.2.0 in /opt/
paiadd upgrade weather-pai          # three-way diff defaults; bumps .meta.yaml version
paictl restart weather-pai          # relaunches against the new bundle
```

## Earmarked, deferred

- **Privileged read/write.** A capability system where kernelPAI is the
  sole writer to `/etc/`, `/usr/`, `/opt/`, `/var/lib/memory/`, etc.,
  and workers route mutations through `/root/inbox/` elevation requests.
  Today: convention only.
- **Multi-PAI jailing.** Real isolation via sandbox-exec or a host-level
  mechanism. Pairs with privileged read/write.
- **`/mnt/` guest PAIs.** Mountable, scoped, possibly stateless PAIs.
  Lights up alongside jailing.
- **`paiman` install/release path.** Dev path lands first (`/usr/lib/pais/`);
  install/upgrade/uninstall against `/opt/<pkg>/<ver>/` follows.
- **`/boot/recovery/`.** Snapshot-before-mutate for kernelPAI's edits to
  `/etc/`. Easy to add when self-mutation grows risky.
- **`/dev/`.** External services as device-like endpoints. Probably
  collapses into drivers + events; slot reserved.
- **`/var/cache/`.** Regenerable derived state (embeddings, indexes).
- **Modular kernel composition under `/boot/`.** Hot-swappable kernel
  modules with stable ABI. Driven by real need.
- **Instance migration.** `paiadd export <pai>` → tarball of
  `/var/lib/instances/<pai>/`; `paiadd import` on another host. Trivial
  given the instance is one self-contained directory.

## Open questions

- **Install mechanism: repo → `/`.** The repo is a Python package + git
  repo with conventional Python layout; the installed system is FHS at
  `/`. How install gets from one to the other — symlink `src/` into
  `/usr/src/`? copy at install time? a bootstrap script that lays out
  `/` from the package? — is undecided. Symlink keeps "what humans edit"
  and "what PAI sees" in sync; copy gives reproducible installs.
- **Multiple instances of the same bundle.** Can `weather-pai` be added
  twice with different names? Trivially supported; worth confirming we
  want it.
- **Write-back semantics on per-PAI memory promotion.** When a worker
  PAI learns something canonical, how does it land in `/var/lib/memory/`?
  Direct write through `shared/`? Drop a request to kernelPAI? TBD with
  multi-PAI.
- **Baseline supervision library.** Where does the every-PAI-inherits-it
  internal supervisor live? `/usr/lib/skills/supervisor/`? A Python
  module imported by all bundles? Both?
