# PAI Filesystem v2

> **Status: forward-looking spec.** Supersedes `FILESYSTEM.md` (v1 FHS design)
> and `SCAFFOLDING.md` (v0 layout). Read this for where we're going.

## What changed from v1

- **No `/pai/` nesting.** The whole system is PAI; there are no other tenants
  on this filesystem. So `/usr/share/prompts/` not `/usr/share/pai/prompts/`,
  `/var/lib/memory/` not `/var/lib/pai/memory/`, etc.
- **No usrmerge.** `/bin/` and `/sbin/` are real directories, not symlinks
  into `/usr/`. Linux's usrmerge is a legacy-compat dance PAI doesn't need.
- **FHS as convention, not as kernel.** PAI's tree lives on the macOS host
  as plain directories. We are *not* running inside a Linux container or
  VM — going there would cut PAI off from the native macOS surfaces
  (iMessage, Mail, Calendar, Contacts, Shortcuts, AppleScript, accessibility)
  that make a personal AI useful in the first place. The mental model is
  Unixy; the implementation is files and processes on the host.
- **Privilege is convention, not enforcement.** A real privileged-write
  system (kernelPAI as the only writer to `/etc/`, `/usr/`, `/var/lib/memory/`;
  workers ask via elevation requests) is the eventual shape, but enforcing
  it requires plumbing on every tool call. Until multi-PAI demands it,
  the rules in the privilege table are honored by convention — PAIs *should*
  not write outside their home, but nothing stops them.
- **Jailing deferred.** `/home/<pai>/` and `/root/` exist as convention;
  real isolation (chroot/sandbox-exec/elevation requests) is earmarked for
  when multi-PAI lands.
- **Slots reserved, not built.** `/opt/`, `/boot/recovery/`, `/run/`,
  `/var/cache/` are documented but stay empty until something earns them.

## Top-level tree

```
/
├── boot/recovery/         snapshots before kernelPAI mutations (deferred)
├── bin/                   PAI-callable tools (paictl, paimount, paiman, …)
├── sbin/                  kernelPAI-only tools (self-heal, …)
├── etc/                   config (read by all, written by kernelPAI)
│   ├── config.yaml
│   ├── drivers/<name>/events.yaml
│   └── prompts/           per-install prompt overrides
├── home/<pai>/            per-PAI workspace (jail enforcement deferred)
│   ├── identity.yaml
│   ├── directives.md
│   ├── prompts/           per-PAI prompt customization
│   ├── memory/
│   │   ├── shared → /var/lib/memory/
│   │   └── private/
│   ├── inbox/
│   ├── workspace/
│   └── tmp/
├── root/                  kernelPAI's home (same shape as /home/<pai>/)
├── opt/                   reserved for pacman packages
├── proc/<pai>/<svc>/      supervised services (spec.yaml, status, log.md)
├── sys/drivers/<name>/    live driver runtime state (cursors, last event)
├── tmp/                   system-wide ephemeral, cleared on boot
├── usr/
│   ├── lib/
│   │   ├── drivers/<name>/      driver source code
│   │   ├── skills/<name>/       skill source code
│   │   └── venv/                Python virtualenv (uv-managed)
│   ├── share/prompts/           shipped baseline prompts
│   └── src/                     Python source
└── var/
    ├── lib/memory/              canonical ground truth (multi-PAI shared)
    │   ├── people/<name>/about.yaml
    │   ├── topics/<topic>/
    │   └── journal/<date>/
    ├── log/{kernel,drivers/<name>,pai/<pai>}/
    └── spool/communication/messages/<thread>/<date>.md
```

## Per-directory semantics

### `/boot/`
Kernel boot artifacts and recovery snapshots. Useful version: before
kernelPAI mutates `/etc/`, snapshot to `/boot/recovery/<timestamp>/`; if
the next reload fails, roll back. Ambitious version (modular hot-swap of
kernel components) is deferred until something concrete needs it.

### `/bin/` and `/sbin/`
Privilege-split binaries. `/bin/` is for tools any PAI can call; `/sbin/`
is for kernelPAI-only tools (self-heal scripts, fleet ops).

The three core PAI tools all live in `/bin/` and split by layer — same
mental model as `systemctl` / `adduser` / `apt` on Linux. Each only
knows about its own layer; they compose, they don't overlap.

| Tool | Layer | Operates on |
|---|---|---|
| `paiman` | bundles | `/opt/<pkg>/<ver>/` — install, uninstall, upgrade, list bundles |
| `paimount` | instances | `/var/lib/instances/<pai>/` + `/home/<pai>/` + `/etc/config.yaml` — scaffold, mount, unmount, list PAIs |
| `paictl` | services | `/proc/<pai>/<svc>/` — status, restart, logs, reload (the systemctl analogue) |

`paiman` doesn't know what an instance is. `paimount` doesn't know what
a service is. `paictl` doesn't know what a bundle is. Crossing layers
means composing the tools, not adding flags to one of them.

No usrmerge symlinks — `/bin/` and `/sbin/` are real directories with
real files in them. Linux symlinks them into `/usr/` for legacy `#!/bin/sh`
compatibility; PAI has no such legacy.

### `/etc/`
System config. Read by all PAIs, written only by kernelPAI.

- `etc/config.yaml` — kernel + PAI fleet declaration. Reconciled into
  `/proc/` at boot and on `kernel:reload_config`.
- `etc/drivers/<name>/events.yaml` — per-driver event-kind manifest.
  Source of truth for `wake_on:` patterns.
- `etc/prompts/` — per-install overrides on top of `/usr/share/prompts/`.
  When PAI looks up a prompt, `/etc/prompts/` shadows `/usr/share/prompts/`.

### `/home/<pai>/`
Per-PAI workspace. Each PAI gets one. Layout:

- `identity.yaml`, `directives.md` — who this PAI is, how it behaves
- `prompts/` — per-PAI prompt customization (shadows `/etc/prompts/` and
  `/usr/share/prompts/`)
- `memory/shared/` — symlink into `/var/lib/memory/` for read-through
  access to canonical ground truth
- `memory/private/` — writable, per-PAI memory
- `inbox/` — events and messages addressed to this PAI
- `workspace/` — persistent scratch
- `tmp/` — per-PAI ephemeral, separate from system `/tmp/`

**Jailing deferred.** Today: convention. PAIs *should* only write under
their own home, but nothing enforces it. When containerization lands,
the directory shape already supports a proper jail (chroot, sandbox-exec,
or container) without rearrangement.

### `/root/`
kernelPAI's home. Same shape as `/home/<pai>/`. Once jailing is real,
worker PAIs drop elevation requests into `/root/inbox/` instead of
writing privileged paths directly. Until then, kernelPAI is just the
PAI that happens to handle kernel-level work.

### `/opt/`
Target for `paiman`-installed bundles. Each installed bundle is
self-contained under `/opt/<pkg>/<version>/` (code, default identity,
bundled skills) — immutable once installed. `paimount` then stitches a
bundle into a live PAI by creating `/home/<pai>/` as a view over
`/opt/<pkg>/<ver>/` plus the PAI's mutable instance state under
`/var/lib/instances/<pai>/`. Upgrade replaces the bundle in `/opt/`;
instance state survives untouched. Package state metadata (what's
installed, version, install date) lives in `/var/lib/packages/`. Empty
until `paiman` ships.

For local development, `/opt/` is bypassed entirely: bundle source lives
at `/usr/lib/pais/<name>/` (editable in place) and `paimount` stitches
straight from there. `/opt/` is only for built/installed release artifacts.

### `/proc/<pai>/<service-slug>/`
Per-PAI supervised services. Each service is a directory with
`spec.yaml`, `status`, `log.md` — see `KERNEL.md`. Process state is
namespaced by PAI so multi-PAI service supervision is clean from day one.

### `/sys/drivers/<name>/`
Live driver runtime state — the sysfs analogue. Current poller status,
queue depth, last event timestamp, cursor positions. Read-mostly window
into what's running *right now*. Distinct from driver code (`/usr/lib/drivers/`)
and config (`/etc/drivers/`).

### `/tmp/`
System-wide ephemeral, cleared on boot. Per-PAI ephemerals belong in
`/home/<pai>/tmp/`.

### `/usr/`
Read-only-ish secondary hierarchy: code, libraries, shipped data.

- `usr/lib/drivers/<name>/` — driver source code (Python)
- `usr/lib/skills/<name>/` — skill source code
- `usr/lib/pais/<name>/` — in-development PAI bundle source (manifest +
  defaults). `paimount` stitches from here for the dev path; release
  bundles live at `/opt/<pkg>/<ver>/`.
- `usr/lib/venv/` — Python virtualenv, managed by uv. The source in
  `/usr/src/` runs against this. Regenerable from `pyproject.toml` +
  `uv.lock` but not throwaway — it's the installed library environment,
  which is exactly what `/usr/lib/` is for in real Linux.
- `usr/share/prompts/` — shipped baseline prompts (bootstrap, system,
  skill defaults). Read-only from PAI's perspective; updated when the
  package updates. Per-install overrides go in `/etc/prompts/`,
  per-PAI overrides go in `/home/<pai>/prompts/`.
- `usr/src/` — Python source code (today's `src/`)

### `/var/`
All persistent mutable state owned by the running system.

- `var/lib/memory/` — canonical ground truth, see Memory Layout below
- `var/log/{kernel,drivers/<name>,pai/<pai>}/` — append-only logs
- `var/spool/communication/messages/<thread>/<date>.md` — message queues,
  see Communication Layout
- (`var/cache/`, `var/lib/packages/` reserved; created when something
  uses them)

## Memory layout

Canonical memory lives in `/var/lib/memory/`. One record of who Alice is,
one record of the Bishop trip, one journal entry per day.

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
copy-on-write. If a PAI shouldn't write canonical memory, deny it via
jail policy when jailing is real, not via filesystem gymnastics.

`/home/<pai>/memory/private/` is the PAI's own writable space for
things that don't belong in shared ground truth.

## Communication layout

Messages live in shared spool. Linux's `/var/spool/` is exactly the slot
for mail and message queues; multiple PAIs may converse with the same
contact, so messages aren't private to one PAI.

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

Format unchanged from v1: `[HH:MM] sender: message text`, one line per
message, one file per day, append-only.

Per-PAI inboxes symlink the threads they're subscribed to:

```
/home/<pai>/inbox/
├── alice-smith → /var/spool/communication/messages/alice-smith/
└── weekend-crew → /var/spool/communication/messages/weekend-crew/
```

Which symlinks exist controls which threads a PAI sees.

## Driver layout

Each driver has three locations, each with a different role:

| Path | Role |
|---|---|
| `/etc/drivers/<name>/events.yaml` | Config (event manifest, settings) |
| `/usr/lib/drivers/<name>/` | Source code |
| `/sys/drivers/<name>/` | Live runtime state (cursors, last event, status) |

This is the same separation Linux makes between code (`/usr/lib/`),
config (`/etc/`), and runtime (`/sys/`).

## Prompt resolution order

When PAI loads a prompt, it checks in order:

1. `/home/<pai>/prompts/<name>` — per-PAI customization
2. `/etc/prompts/<name>` — per-install override
3. `/usr/share/prompts/<name>` — shipped baseline

First hit wins. Same pattern as `/etc/` shadowing `/usr/share/` in Debian.

## Earmarked, deferred

These are reserved in the layout but not built yet. Each will be added
when there's a concrete driver:

- **Privileged read/write.** A real capability system where kernelPAI is
  the sole writer to `/etc/`, `/usr/`, `/var/lib/memory/`, etc., and
  workers route mutations through `/root/inbox/` elevation requests. Today
  it's convention only — every tool call would need a capability check to
  enforce, which is too much plumbing pre-multi-PAI.
- **Multi-PAI jailing.** `/home/<pai>/` and `/root/` exist as convention;
  real isolation needs sandbox-exec or similar host-level mechanism.
  Pairs with the privileged read/write system above.
- **`/opt/` + `paiman` + mountable bundles.** PAIs as self-contained,
  downloadable, mix-and-match bundles installed under `/opt/<pkg>/<ver>/`
  by `paiman` and stitched into `/home/<pai>/` by `paimount`. Bundle is
  immutable; instance state at `/var/lib/instances/<pai>/` is sacred.
  Package state metadata at `/var/lib/packages/` when `paiman` ships.
  Dev path (bundle source at `/usr/lib/pais/<name>/`) lands first; the
  `paiman` install/release path follows.
- **`/boot/recovery/`.** Snapshot-before-mutate for kernelPAI's edits to
  `/etc/`. Easy to add when self-mutation grows risky enough to warrant
  rollback.
- **`/dev/`.** External services (Gmail, Telegram, Calendar) as
  device-like endpoints. Probably collapses into drivers + events; the
  slot is reserved but unused.
- **`/run/`.** Lockfiles, sockets, PIDs cleared on boot. Add when
  something needs Unix-socket coordination.
- **`/var/cache/`.** Regenerable derived state (embeddings, indexes).
  Add when something needs caching.
- **Modular kernel composition under `/boot/`.** Hot-swappable kernel
  modules with a stable ABI. Driven by a real need (kernelPAI patching
  itself live), not pre-built.

## Open questions

- **FHS root location relative to the dev repo.** Repo root *is* `/`,
  versus repo contains a subdirectory that *is* `/`, versus symlink
  aliasing. Affects how `pyproject.toml`, `tests/`, `.venv` coexist with
  the agent's perceived world.
- **`/usr/src/` mechanism.** Symlink from repo source vs. install-time
  copy. Symlink keeps "what humans edit" and "what PAI sees" the same;
  install-copy separates them.
- **Write-back semantics on per-PAI memory promotion.** When a worker
  PAI learns something canonical, how does it land in `/var/lib/memory/`?
  Write directly through `shared/`? Drop a request to kernelPAI? TBD
  with multi-PAI.
