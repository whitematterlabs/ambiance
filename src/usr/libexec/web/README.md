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

- **Backend** (`src/usr/libexec/web/pai_web/`: `server.py`, `hub.py`,
  `actions.py`) — stdlib
  HTTP + Server-Sent Events. No third-party web framework. Reuses `boot.*` and
  the TUI's pure parsing helpers (`sbin.tui.state`) so the message format has one
  source of truth. FS changes are picked up via `watchdog` (event-driven, tickless
  — no polling of the kernel), and fanned out to every browser over one SSE stream.
- **Frontend** (`src/usr/libexec/web/`) — React + TypeScript + Vite (pnpm),
  markdown via `react-markdown`. It's a **non-Python sidecar**, so it sits in a
  `libexec/` slot with its own `node_modules/` + `dist/`, not next to the Python
  backend.

Because the surface only *attaches* to the kernel, it does **not** install
itself into the kernel runtime (`~/.pai`). In dev, the server resolves the built
frontend from the repo (`src/usr/libexec/web/dist/`). In a `./paibuild` app, the
frontend is bundled next to the Python package and also mirrored under app
resources. `~/.pai` stays untouched.

The browser→kernel direction is plain `POST /api/*`; the kernel→browser
direction is one long-lived `GET /api/stream` SSE feed.

## Run

One-time frontend build for repo/dev use (also done by `install.sh`; `paibuild`
does this automatically when frontend inputs change):

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
python -m usr.libexec.web.pai_web # --host / --port / --open
```

The surface serves the built frontend and the API from the same origin. Repo/dev
serves `src/usr/libexec/web/dist/`; packaged apps serve the bundled copy.
Nothing is copied into `~/.pai`.

## Dev (hot reload)

```bash
python -m usr.libexec.web.pai_web          # API on :8787
cd src/usr/libexec/web && pnpm dev         # UI on :5173, proxies /api → :8787
```

## API

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/stream` | SSE: `hello`, `procs`, `fleet`, `thread`, `event`, `log`, `provider` |
| GET | `/api/state` | One-shot snapshot (same payload as `hello`) |
| GET | `/api/kernel` | Kernel lifecycle status `{running, pid}` |
| POST | `/api/message` | `{pid, text}` → append day-file line + emit `new_message` |
| POST | `/api/interrupt` | `{pid}` → emit `interrupt` event |
| POST | `/api/shell` | `{pid, cmd}` → run `!cmd`, returns `{lines, rc, ctx_applied}` |
| POST | `/api/provider` | `{key}` → write `provider.yaml` |
| POST | `/api/kernel` | `{action: "start" \| "stop"}` → start/stop the kernel |
| POST | `/api/tts` | `{text}` → server-side TTS proxy; requires `ELEVENLABS_API_KEY` |
| POST | `/api/stt` | `multipart/form-data` with `audio` → server-side STT proxy; requires `OPENAI_API_KEY` |

Voice input defaults to `OPENAI_TRANSCRIBE_MODEL=gpt-4o-mini-transcribe`.
Optional `OPENAI_TRANSCRIBE_LANGUAGE` and `OPENAI_TRANSCRIBE_PROMPT` values are
forwarded to the transcription request.

## Keyboard

- `Enter` send · `!cmd` run shell · `Esc` interrupt the active PAI
- `Ctrl+Tab` / `Ctrl+Shift+Tab` next / prev PAI tab · `Ctrl+1..9` select tab
- `⌘K` / `Ctrl+K` command palette (provider switching)
