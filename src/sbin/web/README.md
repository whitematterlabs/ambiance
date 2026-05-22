# PAI web surface

A browser operator console for PAI, matching the Textual TUI feature-for-feature.
See [CAPABILITIES.md](./CAPABILITIES.md) for the parity checklist.

## Architecture

```
TUI / GUI / other owner surface  <->  kernel  <->  LLM
```

The web surface is an **owner surface**, exactly like the TUI. It *attaches* to a
running kernel: it reads the on-disk FHS state (`/proc`, me-thread day-files,
`run/pai/events/`, `kernel.log`) and performs only the same two writes the TUI
makes — append a line to a me-thread day-file, and drop an event file. It never
spawns, drives, or owns the kernel or its runtime.

- **Backend** (`src/sbin/web/`: `server.py`, `hub.py`, `actions.py`) — stdlib
  HTTP + Server-Sent Events. No third-party web framework. Reuses `boot.*` and
  the TUI's pure parsing helpers (`sbin.tui.state`) so the message format has one
  source of truth. FS changes are picked up via `watchdog` (event-driven, tickless
  — no polling of the kernel), and fanned out to every browser over one SSE stream.
- **Frontend** (`src/usr/libexec/web/`) — React + TypeScript + Vite (pnpm),
  markdown via `react-markdown`. It's a **non-Python sidecar**, so it sits in a
  `libexec/` slot with its own `node_modules/` + `dist/`, not next to the Python
  backend.

Because the surface only *attaches* to the kernel, it does **not** install
itself into the kernel runtime (`~/.pai`). The server resolves the built
frontend from the repo (`src/usr/libexec/web/dist/`), or from an embedded
`usr/libexec/web/dist/` if a shipped app populates one. `~/.pai` stays untouched.

The browser→kernel direction is plain `POST /api/*`; the kernel→browser
direction is one long-lived `GET /api/stream` SSE feed.

## Run

One-time frontend build (also done by `install.sh`):

```bash
cd src/usr/libexec/web
pnpm install
pnpm build
```

Then start the kernel + web surface together (parallel to `pai start` for the TUI):

```bash
pai start --web                  # boots kernel, serves UI at http://127.0.0.1:8787, opens a browser
pai start --web --port 9000      # custom port
pai start --web --no-open        # don't auto-open a browser
```

To attach the web UI to an already-running (e.g. headless) kernel, run the
surface on its own — it never boots or owns the kernel:

```bash
python -m sbin.web               # --host / --port / --open
```

The surface serves the built frontend (`src/usr/libexec/web/dist/`) and the API
from the same origin — read straight from the repo, nothing copied into `~/.pai`.

## Dev (hot reload)

```bash
python -m sbin.web                         # API on :8787
cd src/usr/libexec/web && pnpm dev         # UI on :5173, proxies /api → :8787
```

## API

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/stream` | SSE: `hello`, `procs`, `fleet`, `thread`, `event`, `log`, `provider` |
| GET | `/api/state` | One-shot snapshot (same payload as `hello`) |
| POST | `/api/message` | `{pid, text}` → append day-file line + emit `new_message` |
| POST | `/api/interrupt` | `{pid}` → emit `interrupt` event |
| POST | `/api/shell` | `{pid, cmd}` → run `!cmd`, returns `{lines, rc, ctx_applied}` |
| POST | `/api/provider` | `{key}` → write `provider.yaml` |

## Keyboard

- `Enter` send · `!cmd` run shell · `Esc` interrupt the active PAI
- `Ctrl+Tab` / `Ctrl+Shift+Tab` next / prev PAI tab · `Ctrl+1..9` select tab
- `⌘K` / `Ctrl+K` command palette (provider switching)
