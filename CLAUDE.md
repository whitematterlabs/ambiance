# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

PAI (Personal AI) — an always-on AI agent fleet that uses the filesystem as its primary data structure. This branch (`linux`) is **v4**: an org hivemind on a single-tenant Linux box, PAIs as Unix users, systemd as the supervisor.

The repo holds exactly five things:

- `src/agent/` — the member-plane runtime (Python): turn engine, tools, prompt assembly, spool messages, provider backends. Entrypoint is `python -m agent`; there are no console scripts.
- `src/broker/` — pai-broker (Rust, cargo). Dormant but resident; owns `broker.sock`.
- `src/usr/lib/{systemd,sysusers.d,tmpfiles.d}` + `src/usr/libexec/provision-member` + `src/etc/pai/` — systemd units (`pai@.service`, `pai-broker.service`, broker preset), member provisioning, and the `/etc/pai` seed files (commented-hint `config.yaml` + `env`).
- `src/usr/share/doc/` — `FILESYSTEM_v4.md` (authoritative layout spec) and `MIGRATION_v4.md` (the v3→v4 port ledger).
- `image/` — the box image build (`image/build` + mkosi config): stages broker, python runtime, agent venv, units and seeds, then assembles stock Ubuntu noble around them.

## Hard rules

- **No PAI_ROOT, no env root/prefix, ever.** v4 uses literal absolute paths: `/etc/pai`, `/var/lib/pai`, the member Unix user's `~`. Code that threads a configurable root through is a regression.
- The v3 monolith (kernel, paiman/paictl bins, web console, mac drivers, its tests) was deleted from this branch on 2026-07-20. It lives on `main` and in `~/Projects/pairegistry/`. The unported v3 rows (scheduler→timerfd, subagents, skills) port **from git history on `main`**, not from any live source tree here. The claudecode backend is dead, not deferred — this branch is pure VPS.
- The v4 console is a later, from-spec rebuild — do not resurrect the v3 web frontend.

## Dev loop

Dev and tests run on the Linux VM, as root:

- Enter: `orb -m pai-linux` (OrbStack machine; repo is shared from the mac host).
- Venv: `UV_PROJECT_ENVIRONMENT=$HOME/pai-venv` (keeps the venv out of the shared tree).
- Sync deps: `uv sync`. Tests: `uv run python -m pytest`.
- Broker: `cargo build` in `src/broker/`.
- Live system venv on the VM: `/usr/lib/pai/venv` (`uv pip install --python /usr/lib/pai/venv/bin/python -e .`); members run under systemd (`systemctl restart pai@<member> pai-broker`).
- Image: `image/build` on the VM as root (needs cargo, uv, mkosi — mkosi via `uv tool install git+https://github.com/systemd/mkosi.git@v25.3`). Output `/var/lib/pai-image/out/pai` (builder-local disk — the shared repo tree can't hold image ownership); boot-test with `systemd-nspawn -bD`.

Quick mac-side checks (`uv sync`, pytest, `uv build`) work too — everything except the Linux-only wake loop.

## Reporting back

Be terse. When you finish (or pause) work, surface exactly three things and nothing else:
- **Did** — what changed (files/behavior), in a line or two.
- **Bugs/unhandled** — anything broken, skipped, or not covered. Don't bury it.
- **Status** — done / blocked / needs-decision, and the single next step if any.

No preamble, no re-explaining the request, no option menus unless a decision is genuinely blocked.

## Design principles

- Plain text over databases — everything greppable, tailable, appendable.
- Tickless and event-driven — no polling, no heartbeat; sleep on inotify/timerfd.
- systemd owns process lifecycle; the agent owns nothing but its own turn.
