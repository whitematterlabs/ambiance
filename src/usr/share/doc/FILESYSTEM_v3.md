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
- **Four-tool split.** `paiman` (bundles) / `paiadd`+`paidel` (configure
  instances) / `paictl` (instance runtime: flip `active:` on fleet entries) /
  `paicron` (services: cron jobs, watchers, async work). Each tool owns
  one noun, one layer, one path. No flags that cross layers.
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
  `/`. Install lays the repo's contents into FHS slots: **kernel source
  goes to `/boot/`** (the kernel is not a userspace program — it does
  not live under `/usr/`); userspace source (drivers, skills, PAI
  bundles) goes under `/usr/lib/`; shipped prompts go to
  `/usr/share/prompts/`; PAI-callable CLI tools go to `/bin/` and
  fleet-mutation / kernel ops go to `/sbin/`; and so on.

> **Hard rule.** `/boot/` and `/usr/` are not interchangeable. Kernel
> code (the supervisor and its helper libraries) belongs in `/boot/`.
> Anything a PAI or a driver runs against (drivers, skills, bundles,
> shipped data) belongs in `/usr/`. If you find yourself reaching for
> `/usr/src/` or `/usr/lib/` for kernel code, stop — that's a layering
> mistake. Likewise, do not put driver code in `/boot/`.

Everything in this document describes the **installed system**, not the
repo. The repo is the build input; `/` is the runtime.

How install actually works (symlink the repo's `src/boot/` into `/boot/`,
copy from `~/Projects/pairegistry/drivers/<name>/` into `/usr/lib/drivers/<name>/`
via `paiman install`, etc.) is an implementation detail tracked in Open Questions.

**Userspace packages (drivers, skills, libs, pais, prompts beyond the seed three) do NOT live in this pyproject repo.** They live in `~/Projects/pairegistry/` and are installed via `paiman install <name>`.

### Today's `src/` → FHS slots

| Source (today) | Destination | Notes |
|---|---|---|
| `src/pai.py` | `~/.pai/sbin/init` | Refactored into the kernel entrypoint |
| `src/boot/` | `~/.pai/boot/` | Kernel source — the supervisor and its helper libraries (PID 1's image). Not userspace; never under `/usr/`. |
| `~/Projects/pairegistry/drivers/<name>/` | split two ways | Code + shipped manifest → `/usr/lib/drivers/<name>/` (events.yaml ships here, not in /etc/). Live runtime state → `/sys/drivers/<name>/`. Driver enable/disable rides on `/proc/<slug>/spec.yaml` `active:` like any other process. *Source lives in pairegistry, not this pyproject repo.* |
| `src/bin/` | split by privilege | PAI-callable shims (`paictl`, `paicron`, `send-message`, `subagent`, …) → `~/.pai/usr/bin/` (`/bin/` is a symlink to `usr/bin/`). Fleet-mutation / kernel ops (`paiman`, `paiadd`, `paidel`, `paifs-init`) → `~/.pai/sbin/`. Split lives in `SBIN_SCRIPTS` in `bin/paifs_init.py`. |
| `src/sbin/tui/` | `~/.pai/sbin/tui` | Shared parsing/watcher helpers (`state.py`) imported by the web console. The terminal client itself is deprecated (`deprecated/tui/`); the web console is the sole owner surface. |
| `src/sbin/migrate.py` | `~/.pai/sbin/migrate` | One-shot kernelPAI op |
| `src/sbin/reset.py` | `~/.pai/sbin/reset` | One-shot kernelPAI op — destructive; wipes runtime state |
| `src/sbin/reboot.py` | `~/.pai/sbin/reboot` | Emits `kernel:restart`; kernel drains in-flight nudges, gracefully stops drivers, then `os.execvp`s itself in place (PID 1 preserved). Use to apply on-disk patches to kernel-imported modules. |
| `src/usr/libexec/web/` | `~/.pai/usr/libexec/web/` | Web owner surface sidecar: backend package, frontend source, and built assets. Invoked by `pai start --web`; not a `/sbin/` source tree. |
| `src/prompts/` | `~/.pai/usr/share/prompts/` | Shipped baseline prompts |
| `src/usr/share/doc/` | `~/.pai/usr/share/doc/` | Shipped documentation. The runtime slot is a **real directory of per-file symlinks** into the shipped source (repo checkout in dev, `opt/pai/<ver>/` in tarball installs), plus `built` → `/var/lib/doc/built/` for durable PAI-authored docs — never one whole-dir symlink into the rotating release dir |
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

## The four tools

```
/sbin/paiman  ── bundles            (apt analogue)
/sbin/paiadd  ── instance config    (useradd analogue)   /sbin/paidel
/bin/paictl   ── instance runtime   (the `active:` flag in /etc/config.yaml)
/bin/paicron  ── services           (systemctl/cron analogue for /proc/<svc>/)

Bundle / instance ops (paiman, paiadd, paidel) mutate the fleet and
live in /sbin/. Runtime ops (paictl, paicron) only flip status files
in /proc/ and live in /bin/.
```

| Tool | Operates on | Verbs |
|---|---|---|
| `paiman` | `/opt/<pkg>/<ver>/` | `init`, `install`, `uninstall`, `upgrade`, `list` |
| `paiadd` / `paidel` | `/home/<pai>/`, `/var/lib/instances/<pai>/`, `/etc/config.yaml` | `paiadd <name>`, `paidel <name> [--purge]` |
| `paictl` | `/etc/config.yaml` `active:` flag, reconciled into `/proc/<pai>/` | `ls`, `status`, `start`, `stop`, `logs`, `reload` |
| `paicron` | `/proc/<slug>/` (services) | `start`, `stop`, `restart`, `status`, `ls`, `logs` |

`paiman` doesn't know what an instance is. `paiadd` doesn't know what a
process is. `paictl` doesn't know what a bundle is. `paicron` doesn't
know what a PAI is. Crossing layers means composing tools, not adding flags.

`paictl` operates at PAI granularity only — it flips `active: true|false`
on a fleet entry and emits `kernel:reload_config`; the kernel's reconcile
spawns or stops the proc. Want to restart a driver inside email-pai?
Stop and start email-pai, or send it a message asking it to recycle the
driver. Internal supervision is the PAI's job, not kernelPAI's.

## Top-level tree

```
/
├── boot/                  kernel source code (the supervisor "image")
│   └── recovery/          snapshots before kernelPAI mutations (deferred)
├── bin/ → usr/bin/        symlink — one bin for PAI-callable tools
├── sbin/                  kernelPAI-only tools, plus init
│   └── init               entrypoint — execs into the kernel as PID 1
├── etc/                   config (read by all, written by kernelPAI)
│   ├── config.yaml        fleet declaration: list of PAIs
│   ├── drivers/<name>/events.yaml
│   └── prompts/           per-install prompt overrides
├── home/<pai>/            per-PAI workspace — stitched symlink tree
│   ├── memory/
│   │   ├── shared         → /var/lib/memory/
│   │   ├── private        → /var/lib/instances/<pai>/memory/private/
│   │   ├── skills         → /usr/lib/skills/
│   │   └── doc            → /usr/share/doc/
│   ├── bin                → /usr/bin/
│   ├── inbox              → /var/lib/instances/<pai>/inbox/
│   ├── workspace          → /var/lib/instances/<pai>/workspace/
│   ├── tmp/               real dir, ephemeral
│   └── <bundle-links>     bundle-declared channel views (e.g. mail/, drafts/)
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
│   ├── share/
│   │   ├── prompts/             shipped baseline prompts
│   │   └── doc/                 real dir; per-file links into the shipped docs
│   │       └── built            → /var/lib/doc/built/ (durable PAI-authored docs)
│   └── src/                     Python source (kernelPAI, libraries)
└── var/
    ├── lib/
    │   ├── doc/built/           durable owner/PAI-authored docs (capability help pages)
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
Privilege-split binaries. `/bin/` = any PAI may call (e.g. `paictl`,
which only flips status files in `/proc/`). `/sbin/` = kernel/owner
ops that mutate `/etc/`, the fleet, or system state — `paiman`,
`paiadd`, `paidel`, plus self-heal tools and the system entrypoint.

`/bin/` itself is a relative symlink to `usr/bin/` — one bin for
PAI-callable tools, modern-distro style. The meaningful split is
`/bin/` vs `/sbin/`, not `/bin/` vs `/usr/bin/`.

`/sbin/init` is the entrypoint. It's a thin shim that verifies
`~/.pai/` layout, then `exec`s into the kernel from `/boot/`. After
exec, PID 1 *is* the kernel — there is no separate init process
hanging around. Boot phases (driver probe, fleet reconcile,
kernelPAI start, fleet start) are the kernel's first-phase
startup, not a pre-handoff script.

Boot sequence:

1. **Sanity check** — verify required dirs exist (`etc/`, `var/lib/`,
   `proc/`, `run/`); bail loudly if not.
2. **Clean ephemeral state** — wipe `/tmp/` and `/run/pai/events/`.
   Stale `/proc/<name>/` and `/run/pais/<name>/` dirs are *not*
   wiped here — they are declared-state mirrors managed by `paiadd` /
   `paidel`. A future PID-keyed proc layer may take this over.
3. **Driver probe** — for each driver shipped under
   `/usr/lib/drivers/`, run a `health()` check (events.yaml readable,
   code importable, credentials present). Log to `/var/log/kernel/boot.log`.
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
- `etc/prompts/` — per-install overrides on top of `/usr/share/prompts/`.

There is no `etc/drivers/`. Drivers are a code-time registry in the
kernel; their shipped manifest (`events.yaml`) lives next to the code
at `/usr/lib/drivers/<name>/events.yaml`, and their runtime state
lives in `/proc/<slug>/` like any other process. `/proc/<slug>/spec.yaml`
carries an `active:` flag (default true) that paictl flips to start/stop
them. Reconcile is event-driven via `kernel:reload_config` — no polling.

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
- `inbox/` — name-addressed message drop for send_message
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
Distinct from driver code + manifest (`/usr/lib/drivers/`).

Unchanged from v2.

### `/tmp/`
System-wide ephemeral, cleared on boot. Per-PAI ephemerals belong in
`/home/<pai>/tmp/`.

### `/usr/`
Code, libraries, shipped data.

- `usr/lib/drivers/<name>/` — driver source code (Python).
- `usr/lib/skills/<name>/` — skill source code.
- `usr/lib/pais/<name>/` — **in-development PAI bundle source.**
  `paiadd` stitches directly from here for the dev path. `/opt/` is
  bypassed entirely; the source tree IS the bundle, edited in place.
- `usr/lib/venv/` — Python virtualenv.
- `usr/libexec/<name>/` — **sidecar helpers and internal app surfaces**:
  Node bridges, Rust binaries, Go agents, owner UIs, anything driven by
  a public command but not itself a user-facing source tree. FHS-faithful
  split: `usr/libexec/web/` holds the web owner surface, while
  `usr/lib/drivers/<driver>/` stays Python-only; the bridge sources
  + manifest (e.g. `package.json`, `Cargo.toml`) live at
  `usr/libexec/<driver>/`. Installed dependency trees (`node_modules/`,
  `target/`, `vendor/`) live here too — generated at install time,
  never committed in pairegistry. The driver's `package.yaml` declares
  the install step paiman runs to populate this dir.
- `usr/share/prompts/` — shipped baseline prompts.
- `usr/share/doc/` — documentation view. A **real directory** holding one
  symlink per shipped doc (into the repo checkout in dev, or the current
  `opt/pai/<ver>/src/usr/share/doc/` release dir in tarball installs) plus
  `built` → `/var/lib/doc/built/`, the durable slot for owner/PAI-authored
  docs. Deliberately *not* a single whole-dir symlink into the release dir:
  releases rotate on `pai update` (old `opt/pai/<ver>` dirs are GC'd), so
  anything written through such a link would be destroyed. Shipped links
  are re-pointed by paifs-init on every provision/update; `built/` content
  never moves. The kernel's doc-watcher watches both this dir and
  `/var/lib/doc/`, so a doc dropped in either place nudges the librarian.
- `usr/src/` — userspace Python source: shared libraries used by
  drivers, skills, and PAI bundles. **Kernel code does not live here**
  — the kernel image is `/boot/`.

### `/var/`
All persistent mutable state.

- `var/lib/memory/` — canonical ground truth (see Memory Layout).
- `var/lib/doc/built/` — **durable owner/PAI-authored documentation**
  (e.g. the capability help pages root writes after `grow-capability`).
  Exposed to every PAI at `/usr/share/doc/built/` (and through the home
  view at `memory/doc/built/`) via a symlink; the files live here in
  `/var` so they survive `pai update` release rotation. paifs-init
  migrates any real files stranded at the legacy in-release
  `usr/share/doc/built/` location into this slot, idempotently.
- `var/lib/instances/<pai>/` — **per-PAI mutable instance state.** This
  is what survives uninstall/reinstall. Sacred.
- `var/lib/packages/` — paiman metadata (deferred).
- `var/lib/<name>/` — **package-owned state slot.** Any installable
  package (driver, subagent, lib, …) may claim `/var/lib/<name>/` for
  its own persistent state, as long as it creates and owns the directory
  in its `libexec/install.sh` hook. The kernel skeleton stays minimal;
  packages provision their own dirs at install time. Existing precedent:
  drivers like `imessage` and `whatsapp` keep cursors and per-domain
  state under `/var/lib/<driver>/`; the `browse` subagent stores
  imported Chrome cookies under `/var/lib/browse/cookies/` and its
  dedicated CDP-mode Chrome profile under
  `/var/lib/browse/chrome-cdp-profile/`.
- `var/log/` — append-only logs.
- `var/spool/communication/` — message queues (see Communication Layout).
- `var/spool/approvals/` — channel-agnostic owner-approval queue. One yaml
  per outbound send an `email`/`imessage` driver blocked because the
  capability is in `ask` mode; the `approvals` driver watches it and
  delivers what the owner approves.
- `var/cache/` — regenerable derived state (deferred).

## Bundle anatomy

A bundle is the template a PAI is instantiated from.

```
/opt/<pkg>/<version>/                     (release; from paiman install)
/usr/lib/pais/<name>/                     (dev; authored in place)
├── package.yaml       what this PAI declares it needs and provides
└── prompt.md          role prompt for this PAI
```

The `package.yaml` manifest declares:

- bundle name, version, description
- required drivers (by name, with version constraints)
- required skills (same)
- requested capabilities (which paths it needs read/write — informational
  until a capability system enforces them)
- default instance name (overridable at `paiadd` time)
- `home.links` — symlinks the kernel stitches into the instance's home
  (channel views like `mail/`, `drafts/`, `messages/`). The kernel only
  seeds universals (`bin`, `inbox`, `workspace`, `memory/*`, `tmp`); the
  bundle owns everything else. Targets are relative to `PAI_ROOT` and
  cannot escape it; link names cannot collide with the universals.

### Driver mounting — which PAI gets which driver

A PAI "mounts" a driver when the driver's own `home.links` (declared in
`/usr/lib/drivers/<name>/package.yaml`) get stitched into that PAI's
home. The kernel decides the mounted set per slug at stitch time
(`mounted_drivers_for` in `src/boot/stitch.py`), and there are exactly
three rules:

1. **Fallback PAI** (`fallback: true` in `/etc/config.yaml`) — mounts
   *every* installed driver. It is the last-resort handler for any
   unrouted event, so it must see every surface.
2. **Bundled PAI** — mounts the intersection of its bundle's `deps:`
   list and the locally-installed drivers. A driver listed in `deps:`
   that isn't installed is simply not mounted (no error).
3. **Bundleless PAI** (e.g. `root`) — mounts no drivers.

Consequences worth knowing:

- `deps:` is doing double duty — it is both the paiman install list
  *and* the driver-mount list. There is currently no way to depend on a
  driver (so a skill can use it) without also mounting its `home.links`,
  and no per-instance override: two instances of the same bundle always
  mount the same driver set. Changing that means a new manifest field,
  not a config tweak.
- A driver's `home.link` name colliding with a bundle/seed link is a
  hard error at stitch time — drivers cannot silently shadow
  bundle-declared paths.

**Drivers and skills are system-shared dependencies, not bundle-vendored.**
A bundle declares what it needs; `paiman` resolves and installs the
required drivers into `/usr/lib/drivers/<name>/` and skills into
`/usr/lib/skills/<name>/` if they're not already there. Two PAIs that
both need the `gmail` driver share one installed copy. Version pinning
in the manifest handles ABI drift; the system can hold multiple installed
versions of a skill/driver if bundles disagree.

This means a bundle is small — mostly a manifest, plus any defaults it
wants to seed into a new instance. The heavy code (drivers, skills) is
shared infrastructure managed by `paiman` at the system layer, not
duplicated per-bundle.

Bundle content is **immutable** post-install. Edits go to instance state.

## Instance anatomy

An instance is a configured PAI: a name, an identity, private memory,
accumulated state.

```
/var/lib/instances/<pai>/
├── .meta.yaml         { bundle, version, source, added_at }
├── memory/private/    PAI's own writable memory
├── workspace/         persistent scratch
└── inbox/             events addressed to this PAI
```

A PAI's identity (name, owner, role) is not stored as a text file in the
home or instance — it's already in `/etc/config.yaml` (declared) and
`/proc/<pid>/spec.yaml` (live). Behavioral guidance accumulates in
`memory/private/` like any other learned context, not as a monolithic
`directives.md`. Per-instance prompt overrides happen by pointing
`config.yaml`'s `prompt:` at a different file under `/usr/share/prompts/`,
not by stashing prompts in the instance.

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
Layout is **flat files** — every record is a single greppable markdown or
yaml file, not a dated directory tree.

```
/var/lib/memory/
├── MEMORY.md                       # one-line index: ## Topics / ## People / ## Projects
├── people/<slug>/
│   ├── about.yaml                  # identity stub, contacts-driver owned: {name, handles, relationship, entry}
│   └── profile.md                  # librarian-owned living rollup: Summary + dated Facts + Open (created on signal)
├── projects/<slug>/project.md      # long-running effort: Summary / Timeline / Decisions / Open (status frontmatter)
├── topics/<slug>.md                # standalone durable facts (owner prefs, routing discoveries)
└── journal/
    ├── <YYYY-MM-DD>.md             # librarian's reconstructed episodes for the day + audit lines
    └── archive/<year>.md           # rotated journals (shared >30d), appended one file per year
```

- **People**: `about.yaml` is the identity stub (first-write-wins, written by
  the contacts driver). `profile.md` is the librarian-owned rollup — created
  only when a person accumulates durable signal.
- **Projects**: an effort graduates from `topics/` to `projects/` once it has a
  timeline and ≥1 participant. `status:` is `active|paused|done|dropped`.
- **Entity files are hybrid**: Summary/Open sections are rewritten each
  consolidation run (current truth); Facts/Timeline/Decisions are append-only
  dated bullets (history). `last_updated:` frontmatter is the freshness signal.
- **Links**: `[[slug]]` inline (bare slug resolves people → projects → topics).
  Backlinks are not stored — grep them: `rg "\[\[<slug>\]\]" memory/`.
- **Journals are reconstructed retroactively** by `librarian` at 3am: it
  reads the prior day's comms archives (messages/email) and writes the episodic
  record itself, then threads durable episodes into the people/project files.
  Fleet PAIs do not journal directly. See `pais/librarian/prompt.md`.

The earlier dated-directory shape (`topics/<topic>/<date>/summary.md`,
`journal/<date>/notes.md`) was never implemented — the runtime has always been
flat. This section now documents the flat reality.

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

### Image refs

Any `![alt](path)` marker — in a message file, peer message body, or
tool stdout returned to the model — is expanded into an Anthropic
image content block when the text is delivered to the LLM. Paths are
tilde-expanded and resolved relative to the PAI's home (for inbound
messages) or cwd (for tool_result); after resolution they must stay
inside `PAI_ROOT`. Missing files, escapes, and unsupported types
(non png/jpeg/gif/webp) pass through as literal markdown so the model
sees what was attempted. `browse screenshot` emits this marker on its
final stdout line, so screenshots auto-attach without a flag.

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
| `/usr/lib/drivers/<name>/` | Code + shipped `events.yaml` manifest |
| `/sys/drivers/<name>/` | Live runtime state |
| `/proc/<slug>/spec.yaml` | Per-driver `active:` flag (paictl-flippable) |

Drivers ship as code-owned bundles under `/usr/lib/drivers/`; runtime
state is split between `/sys/drivers/` (driver-internal cursors) and
`/proc/<slug>/` (kernel-managed lifecycle, same as PAIs). There is no
`/etc/drivers/` — the kernel's driver registry is the source of truth
for which drivers exist.

## Event vocabulary

Events are the kernel's routing currency. Every event has a `kind:`
string of the shape `<namespace>:<name>` — `imessage:new`,
`gmail:incoming`, `kernel:reload_config`, etc. The namespaces:

- **Driver kinds** — declared in `/usr/lib/drivers/<driver>/events.yaml`
  under the `events:` list. That file is the contract: any kind a
  driver emits must appear there with a `description` and `payload`
  shape. Reading `events.yaml` is the canonical way to learn what
  kinds exist.
- **Kernel kinds** (`kernel:*`) — emitted by the kernel itself, not
  by a driver. Examples: `kernel:reload_config`, `kernel:reload_failed`,
  `kernel:proc_failed`, `kernel:restart` (drains nudges, gracefully
  stops drivers, then re-execs the kernel in place — emitted by
  `/sbin/reboot`). Handled by the root PAI by default.

A PAI's `wake_on:` list in `/etc/config.yaml` is a list of fnmatch
globs over kind strings. The kernel fan-outs each event to every PAI
whose `wake_on:` matches; if zero PAIs match, every PAI with
`fallback: true` is nudged instead; if still none, root catches it.

`paiadd` enumerates known kinds at wizard time so the operator can pick
without grepping events.yaml manually.

## Prompt resolution

When PAI loads a prompt, it walks in order:

1. `/etc/prompts/<name>` — per-install override
2. `/usr/share/prompts/<name>` — shipped baseline

First hit wins. Same pattern as Debian's `/etc/` shadowing `/usr/share/`.
Per-instance overrides happen by pointing the PAI's `prompt:` field in
`/etc/config.yaml` at a different file, not by per-home prompt dirs.

## Workflows

### Local-dev: kernelPAI scaffolds a new PAI

```bash
# Scaffold the bundle source
paiman init email-pai
# Creates /usr/lib/pais/email-pai/ with manifest stub + defaults stubs

# kernelPAI authors the bundle:
#   /usr/lib/pais/email-pai/package.yaml       (deps, defaults)
#   /usr/lib/pais/email-pai/prompt.md          (role prompt)

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
paictl stop weather-pai && paictl start weather-pai   # relaunches against the new bundle
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
  `/`. How install gets from one to the other — symlink `src/boot/`
  into `/boot/` and copy `~/Projects/pairegistry/drivers/<name>/` into `/usr/lib/drivers/<name>/` via `paiman install`? a bootstrap script that lays out `/` from the package?
  — is undecided. Symlink keeps "what humans edit"
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
