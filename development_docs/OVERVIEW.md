# Overview

Orientation for anyone working on PAI. Read this first; it's a map, not a manual. Detail lives in sibling docs and in `src/usr/share/doc/`.

## What PAI is

PAI (Personal AI) is an always-on AI agent that uses the **filesystem as its primary data structure**. There is no database; state is plain text on disk, arranged as a quasi-Linux filesystem.

Four principles drive every design decision:

- **Plain text over databases** — everything is greppable, tailable, appendable.
- **Symlinks over duplication** — one source of truth, linked into many contexts.
- **Config is the source of truth** — `/etc/config.yaml` declares the fleet; the runtime reconciles itself from it.
- **The kernel routes events; it does not know what a "message" is.** On-disk shape decisions belong to drivers, not the kernel.

## The two repos

The single most common mistake is editing the wrong repo. There are two:

- **`pai/`** (this repo) — the **kernel and the privileged tools that wrap it**, nothing else.
- **`pairegistry/`** (`~/Projects/pairegistry/`) — **all userspace packages**: drivers, skills, libs, prompts, and PAI bundles. This is their canonical source. `paiman install <name>` copies/symlinks a package into the runtime.

One-line rule for deciding where a change belongs:

> Is it kernel code or a privileged wrapper of the kernel? → `pai/`.
> Is it a driver, skill, lib, prompt, or PAI bundle? → `pairegistry/`.

(The only prompts in this repo are the three seeds the kernel needs to boot, and even those are symlinks into installed registry copies.)

## The runtime is a filesystem

PAI runs against `$PAI_ROOT` (defaults to `~/.pai`), a quasi-Linux FHS. The repo's source is provisioned into this layout by `paifs-init`. The authoritative spec is `src/usr/share/doc/FILESYSTEM_v3.md` — it overrides anything here that drifts.

Quick map of the top level:

| Path | Holds |
|---|---|
| `/boot` | the kernel image (PID 1 supervisor + the libraries it links) |
| `/usr` | userspace — drivers, skills, PAI bundles, shipped data |
| `/sbin` | **root's** tools for managing the runtime (`init`, `reboot`, `paiman`, `paiadd`, `paidel`, …) |
| `/bin` | PAI-callable tools (`paictl`, `paicron`, `send-message`, …); a symlink to `usr/bin/` |
| `/etc` | config — `config.yaml` declares the fleet |
| `/proc` | running processes (one dir per running PAI/driver) |
| `/sys` | driver-internal runtime state |
| `/var`, `/home`, `/opt` | instance state, stitched home views, released bundles |

Kernel code never lives under `/usr`; userspace never lives under `/boot`.

## Core mental models

- **Owner vs root.** The **owner** is the human. **root** is the privileged system identity that manages the runtime. They are distinct — `/sbin` is root's, not the owner's.
- **Bundle → Instance → Process.** A *bundle* is a template (`/opt/<pkg>/<ver>/` released, or `/usr/lib/pais/<name>/` dev). An *instance* is a configured PAI (`/var/lib/instances/<pai>/` + `/home/<pai>/`). A *process* is a running PAI (`/proc/<pai>/`).
- **Tickless / event-driven.** No polling, no heartbeat. The main loop sleeps until a filesystem event fires or the next timer is due.
- **Drivers own external surfaces.** Anything that owns the on-disk shape of an external surface (messages, email, calendar, contacts) is a driver — never the kernel.

## The four management tools

One tool per layer:

- **`paiman`** — bundles (install/manage templates).
- **`paiadd` / `paidel`** — configure / remove instances.
- **`paictl`** — instance runtime: start/stop fleet members via their `active:` flag.
- **`paicron`** — services: cron jobs, watchers, async work.

## Surfaces

How the owner sees and drives PAI:

- **TTY** — the terminal; the current daily driver while the kernel's contract with the world is still moving.
- **TUI** — `sbin/tui`, the in-terminal console.
- **Web** — a Vite/React site (frontend in `src/usr/libexec/web`, backend `pai_web` alongside it), a browser console mirroring the TUI. Launched with `pai start --web` (loopback TCP, default port 8787). It ships a web manifest, so it installs as a PWA.
- **Remote / PWA** *(WIP)* — the web surface can be reached from outside the machine over an opt-in ngrok tunnel: a separate TCP listener (distinct from the local owner surface) with bearer-token auth required on every `/api/*` route except `/api/health`; the ngrok authtoken is set up in-app (first-run or a toggle). The transport plumbing works, but the mobile/PWA interface itself is still rough and not ready for daily use.

Surfaces attach to the runtime; they do not own it.

## Where to go next

- `src/usr/share/doc/FILESYSTEM_v3.md` — authoritative FHS layout spec.
- `CLAUDE.md` (repo root) — hard rules and directory semantics for contributors.
- Sibling docs in `development_docs/` — build/setup, kernel internals, drivers, and surfaces (to be written).
