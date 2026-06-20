# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## STOP — packages do not live in this repo

This pyproject repo holds **only the kernel and the privileged tools that wrap it**: `src/boot/` (kernel), `src/sbin/` (root-only tools), `src/bin/` (PAI-callable tools), `src/usr/share/doc/` (kernel docs), `src/prompts/` (the two seed prompts the kernel needs to boot — `root.md`, `pai_default.md`, `capability-escalation.md`; these are symlinks into the installed registry copies).

**Everything else — drivers, skills, libs, PAI bundles, additional bins/sbins, additional prompts — lives in `~/Projects/pairegistry/`, NOT in this repo.** That is the canonical source. `paiman install <name>` copies/symlinks it into `~/.pai/usr/lib/<kind>/<name>/`.

If you are about to create or edit `src/drivers/`, `src/skills/`, `src/lib/`, `src/pais/`, or anything that looks like a userspace package: STOP. Go to `~/Projects/pairegistry/<kind>/<name>/` and edit it there. There is no `src/drivers/` in this repo and there will not be one.

Quick sanity check before editing anything under `src/`:
- Is it kernel code or a privileged wrapper of the kernel? → edit here.
- Is it a driver, skill, lib, prompt (beyond the three seeds), or PAI bundle? → edit `~/Projects/pairegistry/`.

**pairegistry is upstream — update it first and foremost.** The privileged bins (`paictl`, `paicron`, `paiman`, `paiadd`, `paidel`, `paifs_init`, `pai`, `subagent`, `send-message`, …) are *dual-homed*: the dev source is here in `src/bin/`, but `pairegistry/bin/<name>/` holds the canonical installable copy. When you edit a dual-homed bin in `src/bin/`, immediately sync the file into `pairegistry/bin/<name>/<file>.py` so the registry is never behind. Drift here is real and bidirectional — an audit on 2026-06-17 found `src/bin` ahead on ~12 tools and the registry ahead on `paictl`/`edit_file`. Never assume the `src/bin` copy is current without checking the registry.

## Project

PAI (Personal AI) — an always-on AI agent that uses the filesystem as its primary data structure.

The repo is a Python package + git repo. The **runtime** is a quasi-Linux FHS at `$PAI_ROOT` (defaults to `~/.pai`). See `src/usr/share/doc/FILESYSTEM_v3.md` — that is the authoritative layout spec; it overrides anything here that drifts.

## Hard rules — directory semantics

These are not interchangeable. Do not put kernel code under `/usr/`, and do not put userspace under `/boot/`.

- **`/boot/`** — the kernel image. The supervisor (PID 1, pure Python) and every helper library it links against. The kernel is *not* a userspace program. Repo source for it lives at `src/boot/`.
- **`/usr/`** — userspace. Drivers, skills, PAI bundles, shipped data. Anything a PAI or a driver runs against. Never holds kernel code.
- **`/sbin/`** — kernelPAI / owner-only tools that mutate `/etc/`, the fleet, or system state: `init` (entrypoint that `exec`s into the kernel), `reboot` (re-execs the kernel in place via `kernel:restart`), `paiman`, `paiadd`, `paidel`, `paifs-init`, `migrate`, `reset`, `tui`.
- **`/bin/`** — PAI-callable tools (`paictl`, `paicron`, `send-message`, `subagent`, etc.). `/bin/` is a relative symlink to `usr/bin/`.

## Driver layout

Drivers ship as code-owned bundles, not user-editable config. There is no `/etc/drivers/`. **Driver source lives in `~/Projects/pairegistry/drivers/<name>/`, not in this repo.**

| Slot | Holds | Source of truth |
|---|---|---|
| `/usr/lib/drivers/<name>/` | Source code + shipped `events.yaml` manifest | `~/Projects/pairegistry/drivers/<name>/` (installed via `paiman install <name>`) |
| `/sys/drivers/<name>/` | Driver-internal runtime state (cursors, last event) | written at runtime |
| `/proc/<slug>/` | Kernel-managed lifecycle (status, log, `active:` flag for paictl) | written at runtime |

Drivers are a code-time registry in the kernel (see `DRIVER_SPECS` in `src/boot/main.py`). `paictl start/stop <slug>` flips `/proc/<slug>/spec.yaml` `active:` and emits `kernel:reload_config`; reconcile is event-driven, never polled.

If something owns the on-disk shape of an external surface (messages, email, calendar, contacts), it is a **driver**. It is not kernel.

## Bundle / instance / process

- **Bundle** (template) — `/opt/<pkg>/<ver>/` (release) or `/usr/lib/pais/<name>/` (dev source).
- **Instance** (configured PAI) — `/var/lib/instances/<pai>/` (sacred state) + `/home/<pai>/` (stitched symlink view).
- **Process** (running PAI) — `/proc/<pai>/`.

Four tools, one layer each: `paiman` (bundles) / `paiadd`+`paidel` (configure instances) / `paictl` (instance runtime: start/stop fleet members via `active:` flag) / `paicron` (services: cron jobs, watchers, async work).

## Build & dev

- **Python**: 3.14, managed via uv.
- **Install deps**: `uv sync`.
- **Tests**: `uv run python -m pytest`.
- **Run kernel from FHS root**: `cd ~/.pai && usr/bin/python -m boot run`.
- **Run kernel from repo (dev)**: `uv run python -m boot run`.

`paifs-init` provisions `~/.pai/` from the repo: creates the FHS skeleton, symlinks `/usr/src/`/`/usr/lib/drivers/`/`/usr/share/prompts/` at the live repo, builds a self-contained venv at `/usr/lib/venv/`, and generates console-script shims at `/usr/bin/` and `/sbin/`. Idempotent and non-destructive — safe to re-run after `git pull` to refresh shims/venv. To wipe runtime state, use `reset` (destructive).

## Graduating from TTY to .app

> A SwiftUI `PAI.app` (the `macos/` dir + `paibuild`) once lived in this repo but
> was removed: it required Xcode to build, which broke setup on machines with only
> the Command Line Tools. The notes below are the forward plan for re-introducing a
> native surface, not a description of existing code.

PAI runs in a terminal today. That is deliberate while the kernel's contract with the world is still moving (driver layout, FHS semantics, prompt assembly, send_message shape). An `.app` adds a second surface to keep in sync — window lifecycle, dock, notifications, Sparkle-style updates, code signing, and Location/Contacts/Calendar entitlements as a *bundled identity* instead of borrowed from Terminal — and doing that before the kernel is stable means re-doing it.

Three signals to graduate:

1. **The TUI is the daily driver, not the terminal.** When the owner stops dropping to bare shell to inspect `/proc` or tail logs and lives in the TUI, the terminal is just chrome around it.
2. **Owner-facing things the TTY can't do well.** Native notifications, menubar presence, launch-at-login without a launchd plist, a Location Services prompt tied to *PAI* (not Terminal/iTerm), drag-drop into a PAI's home, global hotkey to summon. Each is a hack in TTY, native in `.app`.
3. **Non-technical users.** Setup today is `uv sync` + `paifs-init` + grant Terminal accessibility. An `.app` is the only path to "double-click, grant once."

Graduate when **2 of 3** hit — specifically when per-PAI native notifications or owner-bound Location/Contacts permissions are wanted, because that's when borrowing Terminal's identity becomes the wrong abstraction rather than just an aesthetic one.

**Intermediate step before a full `.app`:** package the kernel + TUI as a launchd-managed background agent + a thin SwiftUI menubar app that attaches to the running kernel over the existing send_message channel. Native notifications and proper entitlements without rewriting the kernel or committing to a windowed app. The TTY still works for dev.

## Reporting back

Be terse. When you finish (or pause) work, surface exactly three things and nothing else:
- **Did** — what changed (files/behavior), in a line or two.
- **Bugs/unhandled** — anything broken, skipped, or not covered. Don't bury it.
- **Status** — done / blocked / needs-decision, and the single next step if any.

No preamble, no re-explaining the request, no option menus unless a decision is genuinely blocked.

## Design principles

- Plain text over databases — everything should be greppable, tailable, appendable.
- Symlinks over duplication — single source of truth, linked from multiple contexts.
- Config is the source of truth: `/etc/config.yaml` declares the fleet (name, provider, model, prompt, wake_on, fallback). Reconcile rewrites `/proc/<pai>/spec.yaml` from it.
- The kernel routes events; it does not know what a "message" is. On-disk shape decisions belong to drivers.
