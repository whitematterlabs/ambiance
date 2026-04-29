# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

## Project

PAI (Personal AI) — an always-on AI agent that uses the filesystem as its primary data structure. Source code lives in `src/`, the agent's live workspace lives in `home/`.

## Architecture

**Everything is a filesystem.** The agent navigates its world using standard shell primitives. No custom APIs. Relationships are symlinks. Data is plain text (YAML for structured data, Markdown for prose/logs).

### Key directories

- `src/` — agent source code (Python 3.14, managed with uv)
- `home/` — the agent's runtime workspace, structured as:
  - `communication/messages/{contact-or-group}/` — append-only message logs, one file per day (`YYYY-MM-DD.md`)
  - `memory/myself/` — identity (`identity.yaml`) and behavioral directives (`directives.md`)
  - `memory/people/{name}/about.yaml` — structured profiles with freeform wiki entries
  - `memory/topics/{topic}/` — cross-conversation topic tracking with date subdirs, symlinks to source messages, and `summary.md`
  - `memory/journal/{date}/` — daily aggregation with symlinks to day's conversations
  - `memory/skills/` — reusable agent capabilities (TBD)
  - `tmp/` — ephemeral storage
  - `workspace/` — persistent storage

### Symlink conventions

Symlinks express relationships without duplicating data. Thread folders symlink participants to `memory/people/{name}/`. Topic date-dirs symlink to the relevant `communication/messages/` day-files. Journal entries symlink to that day's conversations.

### Message format

```
[HH:MM] sender: message text
```

One message per line, append-only, date in filename. `me` for the agent/owner's messages.

## Build & Dev

- **Python**: 3.14, managed via uv
- **Install deps**: `uv sync`
- **Run**: `uv run python src/<script>.py`

## Design Principles

- Plain text over databases — everything should be greppable, tailable, appendable
- Symlinks over duplication — single source of truth, linked from multiple contexts
- The `home/` directory is the agent's world; `src/` is the machinery that operates on it
- The scaffolding doc (`src/usr/share/doc/SCAFFOLDING.md`) is the authoritative spec for the `home/` directory structure
