# PAI

**PAI is an always-on AI agent fleet for a single-tenant Linux box, built like a small Unix org.**

Each fleet member is a real Unix user. Its home directory is its memory, its
inbox is a spool directory, and systemd is its supervisor. There is no
database and no SaaS backend: the filesystem is the data structure, and
everything a member knows or does is a plain file you can `rg`, `tail`, and
`cat`.

This is v4, a ground-up rebuild on the `linux` branch. The previous macOS
runtime (v3: Python kernel, web console, mac drivers) lives on `main` and is
being ported piece by piece — see
[`src/usr/share/doc/MIGRATION_v4.md`](src/usr/share/doc/MIGRATION_v4.md).

## What's here

- **`src/agent/`** — the member-plane runtime (Python). One process per fleet
  member, started as `python -m agent` by `pai@<member>.service`. It sleeps on
  its spool via inotify, wakes on a delivered message, runs a turn against the
  configured model provider (Anthropic or OpenAI wire formats), and goes back
  to sleep. Tools: bash (a literal PTY), read/write/edit, messaging.
- **`src/broker/`** — pai-broker (Rust). A resident coordinator that owns
  `broker.sock`; dormant in v0 beyond fleet introspection.
- **`src/usr/lib/`** — systemd units (`pai@.service`, `pai-broker.service`),
  `sysusers.d`/`tmpfiles.d` skeletons, and
  [`provision-member`](src/usr/libexec/provision-member) for creating fleet
  members as Unix users.
- **`src/usr/share/doc/`** — [`FILESYSTEM_v4.md`](src/usr/share/doc/FILESYSTEM_v4.md),
  the authoritative layout spec (`/etc/pai`, `/var/lib/pai`, member homes —
  literal paths, no configurable root).

## Development

Dev and tests run on a Linux VM (OrbStack machine `pai-linux`), as root, with
the repo shared from the host:

```bash
orb -m pai-linux
export UV_PROJECT_ENVIRONMENT=$HOME/pai-venv
uv sync
uv run python -m pytest        # agent tests, incl. the Linux-only wake loop
cd src/broker && cargo build   # broker
```

Quick checks (sync, most tests, `uv build`) also pass on macOS.

## License

Apache License 2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).
