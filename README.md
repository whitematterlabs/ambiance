# PAI

PAI is a local-first Personal Artificial Intelligence runtime for macOS. It runs
an always-on AI fleet against a plain-text filesystem, with a small Python
kernel supervising PAIs, drivers, tools, memory, and owner-facing surfaces.

PAI is currently an alpha system for technical users. It is useful, hackable,
and intentionally transparent, but it is not yet a polished consumer app or a
hosted multi-tenant service.

## What PAI Does

- Runs one or more long-lived AI processes as a supervised local fleet.
- Stores runtime state as files under `$PAI_ROOT`, defaulting to `~/.pai`.
- Routes external events from drivers, such as email, calendar, iMessage,
  WhatsApp, notifications, voice, and browser/computer-use tools.
- Exposes owner surfaces through a terminal TUI and a local browser UI.
- Installs userspace capabilities from the companion `pairegistry` repository.
- Keeps state inspectable with ordinary tools like `rg`, `tail`, `cat`, and git.

The core design bias is simple: plain text over databases, symlinks over
duplication, config as source of truth, and an event-driven kernel.

## Current Status

PAI is ready for local development and private alpha use. Public internet
exposure is not ready.

Supported today:

- macOS local runtime
- Python kernel and Textual TUI
- React/Vite local web surface
- Local filesystem-backed runtime at `~/.pai`
- Registry-installed drivers, skills, tools, prompts, and PAI bundles
- API-key based model providers

Not ready yet:

- Hosted service deployment
- Broad non-technical onboarding
- Public remote web access
- Stable signed macOS app distribution
- Strong sandboxing or privilege separation for untrusted code
- Versioned registry release channels

Do not expose `pai start --web` directly to the public internet. The local web
surface includes owner-level controls such as shell execution, kernel lifecycle
actions, provider switching, clone/delete actions, and message sending.

## Requirements

- macOS
- [`uv`](https://docs.astral.sh/uv/)
- Python 3.14, managed through `uv`
- [`pnpm`](https://pnpm.io/), only needed for the browser UI
- At least one model provider API key, such as `ANTHROPIC_API_KEY`,
  `OPENAI_API_KEY`, `DEEPSEEK_API_KEY`, or `ZAI_API_KEY` (z.ai GLM)
- macOS permissions for whichever drivers you enable, such as Contacts,
  Calendar, Messages, Mail, Accessibility, or Microphone

Install `uv` if it is not already on your machine. Install `pnpm` if you want
the web surface.

## Install

End users do not need `uv`, Node/`pnpm`, or `git`. One line installs everything:

```bash
curl -fsSL https://raw.githubusercontent.com/whitematterlabs/pai/main/install.sh | sh
```

The installer downloads a prebuilt release tarball (source + `uv.lock` + a
prebuilt web `dist/`) into `~/.pai/opt/pai/<version>/`, installs the `uv` static
binary if missing, runs `uv sync` against the lockfile (prebuilt wheels — no
compiler), and provisions the runtime. It will:

- Install `uv` automatically if it is not already present.
- Provision the runtime filesystem at `$PAI_ROOT`, defaulting to `~/.pai`.
- Seed the kernel, privileged tools, default prompts, and shims.
- Ask for a default model provider in an interactive shell.
- Offer the interactive `paisetup` capability picker.

Developers work from a checkout instead, which keeps source edits live:

```bash
git clone https://github.com/whitematterlabs/pai.git
cd pai
uv sync && uv run paifs-init
```

(Building the web surface for a dev checkout needs `pnpm`; release tarballs ship
it prebuilt.) Cut a release tarball with `pairelease` (dev box needs `pnpm`,
`git`, and — for `--publish` — `gh`).

After installation, start PAI:

```bash
pai start
```

Start the browser UI instead of the TUI:

```bash
pai start --web
```

Use a custom web port:

```bash
pai start --web --port 9000
```

Run only the kernel:

```bash
pai start --headless
```

## Updating

Check for source updates:

```bash
pai update --check
```

Update:

```bash
pai update
```

For a tarball install (the `curl … | sh` path), `pai update` downloads the
latest release into a new `~/.pai/opt/pai/<version>/`, reprovisions, and
repoints `current` — your runtime state under `etc/`, `var/`, and `home/` is
left untouched. Roll back to the previous version with:

```bash
pai update --rollback
```

For a dev checkout, `pai update` instead pulls the git source, refreshes
dependencies, rebuilds the web frontend, and reprovisions shims. It refuses to
pull over local source changes — commit or stash them first.

## Companion Registry

This repository contains the kernel and privileged tools. Most user-facing
capabilities live in the companion registry:

```text
https://github.com/whitematterlabs/pairegistry
```

The registry contains installable packages:

- `drivers/`
- `skills/`
- `lib/`
- `bin/`
- `sbin/`
- `prompts/`
- `pais/`
- `subagents/`

`paiman install <name>` installs packages from the registry into the local PAI
runtime. The default registry is configured in `paiman`; set `PAIMAN_REGISTRY`
to point at a local checkout or another registry source.

Examples:

```bash
paiman list
paiman install imessage
paiman install email-pai
paisetup
```

## Runtime Layout

PAI provisions a quasi-Linux filesystem under `$PAI_ROOT`, defaulting to
`~/.pai`. Important directories include:

| Path | Purpose |
| --- | --- |
| `/boot` | Kernel image and helpers |
| `/usr` | Installed userspace packages and docs |
| `/sbin` | Privileged runtime management tools |
| `/bin` | PAI-callable tools |
| `/etc` | Fleet and provider configuration |
| `/proc` | Runtime process state |
| `/sys` | Driver runtime state |
| `/var` | Instance state, event spools, memory, logs |
| `/home` | Per-PAI stitched home views |
| `/opt` | Installed package staging area |

The authoritative filesystem spec is
[`src/usr/share/doc/FILESYSTEM_v3.md`](src/usr/share/doc/FILESYSTEM_v3.md).

## Architecture

PAI has three main layers:

- **Kernel**: `src/boot/`, a Python event loop that supervises drivers,
  timers, routing, and PAI subprocesses.
- **Privileged tools**: `src/sbin/` and selected `src/bin/` commands that
  manage install, fleet config, lifecycle, packages, and runtime state.
- **Userspace packages**: drivers, skills, prompts, tools, and PAI bundles
  installed from `pairegistry`.

Owner surfaces attach to the kernel; they do not own it:

- TUI: terminal owner console
- Web: local browser owner console, launched with `pai start --web`
- Headless: kernel only, launched with `pai start --headless`

More architecture detail:

- [`development_docs/OVERVIEW.md`](development_docs/OVERVIEW.md)
- [`src/usr/share/doc/KERNEL_ARCHITECTURE.md`](src/usr/share/doc/KERNEL_ARCHITECTURE.md)
- [`src/usr/share/doc/KERNEL.md`](src/usr/share/doc/KERNEL.md)
- [`src/usr/libexec/web/README.md`](src/usr/libexec/web/README.md)
- [`src/usr/share/doc/PAIMAN.md`](src/usr/share/doc/PAIMAN.md)

## Development

Install dependencies:

```bash
uv sync
```

Run tests:

```bash
uv run python -m pytest
```

Build the web UI:

```bash
cd src/usr/libexec/web
pnpm install
pnpm build
```

Run the web API and Vite dev server separately:

```bash
python -m usr.libexec.web.pai_web
cd src/usr/libexec/web
pnpm dev
```

Reprovision the local runtime after source changes:

```bash
uv run paifs-init --no-setup
```

## Repository Boundaries

Use this repository for:

- Kernel code under `src/boot/`
- Privileged tools under `src/sbin/`
- PAI-callable kernel tools under `src/bin/`
- Kernel docs under `src/usr/share/doc/`
- The local web owner surface under `src/usr/libexec/web/`

Use `pairegistry` for:

- Drivers
- Skills
- Libraries
- PAI bundles
- Subagent bundles
- Installable prompts beyond kernel seeds
- Additional installable bins or sbins

Changing the wrong repository is the most common development mistake. If the
change is a driver, skill, prompt, or PAI bundle, it belongs in `pairegistry`.

## Security And Privacy

PAI is local-first, but it is powerful software. It can read and write local
files, run shell commands through owner-authorized tools, access enabled macOS
surfaces, and call configured model providers.

Before installing or enabling packages:

- Read package manifests and hooks.
- Understand what macOS permissions you are granting.
- Keep API keys in your local environment or `$PAI_ROOT/.env`.
- Do not expose the local web UI to untrusted networks.
- Do not install untrusted registry packages.

The runtime is intended to be inspectable. Logs, process state, config, memory,
and event spools are ordinary files under `$PAI_ROOT`.

## Troubleshooting

If the runtime layout is missing or stale:

```bash
uv run paifs-init --no-setup
```

If the web UI does not load, build it:

```bash
cd src/usr/libexec/web
pnpm install
pnpm build
```

If a package is missing:

```bash
paiman install <name>
```

If you need to inspect the fleet:

```bash
paictl status
paictl logs <name>
ps
```

Kernel logs live under:

```text
$PAI_ROOT/var/log/kernel/kernel.log
```

## License

PAI is licensed under the [Apache License 2.0](LICENSE). See [`NOTICE`](NOTICE)
for attribution.
