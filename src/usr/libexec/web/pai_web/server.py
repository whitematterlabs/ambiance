"""Stdlib HTTP + SSE server for the PAI web surface.

No third-party web framework: `http.server.ThreadingHTTPServer` gives one
thread per request, which is all an SSE stream needs (it blocks on its mailbox).
The browser→kernel direction is plain POST; the kernel→browser direction is a
single long-lived Server-Sent Events stream fed by `hub.Hub`.

Run:  python -m usr.libexec.web.pai_web                       # TCP at http://127.0.0.1:8787 (browser)
      python -m usr.libexec.web.pai_web --port N              # custom port
      python -m usr.libexec.web.pai_web --unix-socket PATH    # in-house (PAI.app via WKWebView)
In dev, run Vite (`pnpm dev`) and let it proxy /api to this server.

The unix-socket mode is what the macOS app uses: WKWebView speaks a custom
`pai://` URL scheme whose handler proxies HTTP over the socket. No loopback
TCP listener, so ngrok-tunneled remote (a separate, opt-in TCP listener) and
the local owner surface are two distinct paths.
"""

from __future__ import annotations

import argparse
import hmac
import os
import socket
import urllib.parse
from email import policy
from email.parser import BytesParser
import json
import sys
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer as _StdlibThreadingHTTPServer
from pathlib import Path
from typing import Callable

from boot.paths import PAI_ROOT, REPO_ROOT, usr_libexec

from . import actions
from . import dashboards
from . import driver_health
from .hub import Hub, Subscriber, read_fleet, read_plan, write_plan


def _frontend_dist() -> Path:
    """Where the built React frontend lives.

    The web surface *attaches* to the kernel from outside; it does not live in
    the kernel's runtime root (~/.pai). Packaged builds prefer assets bundled
    next to this Python package; dev falls back to the repo's libexec sidecar.
    Nothing is injected into ~/.pai.
    """
    package_dist = Path(__file__).resolve().parent.parent / "dist"
    app_resource_dist = None
    for parent in Path(sys.executable).resolve().parents:
        if parent.name == "Resources":
            app_resource_dist = parent / "usr" / "libexec" / "web" / "dist"
            break
    candidates = [package_dist]  # wheel: dist bundled next to the package
    if app_resource_dist is not None:
        candidates.append(app_resource_dist)
    candidates.extend([
        REPO_ROOT / "src" / "usr" / "libexec" / "web" / "dist",  # dev: read from repo
        usr_libexec() / "web" / "dist",                          # shipped: embedded slot
    ])
    for cand in candidates:
        if (cand / "index.html").is_file():
            return cand
    return candidates[0]


FRONTEND_DIST = _frontend_dist()

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
    ".map": "application/json",
}

# Image files under PAI_ROOT that `/api/asset` will surface into the console.
# Screenshots and downloaded images render inline by their absolute path; the
# route is the only way the browser can reach a file the SPA didn't ship.
_ASSET_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".bmp": "image/bmp",
}

# Cap on inline text attachments served by `/api/asset`. Large enough for a
# post, a result.md, or a log tail; small enough that a runaway file can't wedge
# the console. Files beyond this are served truncated with a marker.
_MAX_TEXT_ASSET_BYTES = 256 * 1024

HUB = Hub()
MAX_STT_UPLOAD_BYTES = 30 * 1024 * 1024


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # quiet; the kernel log is the place for noise
        pass

    # -- helpers --
    def _json(self, obj, status: int = 200) -> None:
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _binary(self, data: bytes, content_type: str, status: int = 200, *, cors: bool = True) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        # `/api/asset` reads arbitrary on-disk paths, so it opts out of the
        # wildcard CORS other routes send: the console is always same-origin
        # with the server (local loopback, the ngrok host, or the Vite proxy),
        # so it never needs cross-origin reads — and the wildcard would let any
        # website the owner visits `fetch()` the owner's files out of the
        # loopback server. Same-origin requests ignore this header entirely.
        if cors:
            self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0) or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return {}

    def _read_raw_body(self, *, limit: int | None = None) -> bytes:
        length = int(self.headers.get("Content-Length", 0) or 0)
        if limit is not None and length > limit:
            raise ValueError("request body too large")
        return self.rfile.read(length) if length else b""

    # -- auth --
    def _check_auth(self) -> bool:
        """Whether this request may proceed.

        Only the remote TCP instance sets `server.auth_token` (via PAI.app's
        `--auth-token`); the local unix-socket surface and dev `pai start --web`
        leave it `None` and stay unauthenticated. When a token is set, every
        `/api/*` route requires it — except `/api/health` (so the tunnel can be
        probed) and the static shell (so the page can load and then present the
        token on its own API calls). The token arrives either as
        `Authorization: Bearer <tok>` or, for the header-less SSE `EventSource`,
        as a `?token=<tok>` query param. Compared constant-time.
        """
        token = getattr(getattr(self, "server", None), "auth_token", None)
        if not token:
            return True
        path = self.path.split("?", 1)[0]
        if not path.startswith("/api/") or path == "/api/health":
            return True
        presented = self._presented_token()
        return presented is not None and hmac.compare_digest(presented, token)

    def _presented_token(self) -> str | None:
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return auth[len("Bearer "):].strip()
        _, _, query = self.path.partition("?")
        if query:
            vals = urllib.parse.parse_qs(query).get("token")
            if vals:
                return vals[0]
        return None

    # -- routing --
    def do_OPTIONS(self):
        # CORS stays blanket `*` for MVP: the mobile surface is same-origin over
        # the tunnel (the page and its /api/* calls share the ngrok host), so the
        # wildcard isn't load-bearing for auth — the bearer token is. Tighten to
        # the tunnel origin if cross-origin embedding ever becomes a concern.
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        if not self._check_auth():
            return self._json({"ok": False, "error": "unauthorized"}, status=401)
        path = self.path.split("?", 1)[0]
        if path == "/api/stream":
            return self._stream()
        if path == "/api/health":
            return self._json({"ok": True})
        if path == "/api/kernel":
            return self._json({"ok": True, **actions.kernel_status()})
        if path == "/api/drivers":
            # Per-driver health: computed fresh from disk (proc status, health
            # breadcrumbs, /sys state mtimes). The SSE `drivers` message is the
            # live path; this is the poke-it-with-curl view of the same rows.
            return self._json({"ok": True, "drivers": driver_health.read_rows()})
        if path == "/api/state":
            return self._json(HUB.snapshot())
        if path == "/api/models":
            query = self.path.split("?", 1)[1] if "?" in self.path else ""
            vals = urllib.parse.parse_qs(query).get("pai")
            # models_state reads /etc/config.yaml strictly (ConfigError on a
            # bad hand-edit) — map errors to JSON like do_POST does instead of
            # killing the request with no response.
            try:
                return self._json({"ok": True, **actions.models_state(vals[0] if vals else None)})
            except (KeyError, ValueError) as e:
                return self._json({"ok": False, "error": str(e)}, status=400)
            except Exception as e:  # noqa: BLE001
                return self._json({"ok": False, "error": str(e)}, status=500)
        if path == "/api/scheduled":
            return self._json({"ok": True, "tasks": actions.list_scheduled()})
        if path == "/api/plan":
            # Per-PAI live plan.md keyed by pid — the SSE `plan` message is the
            # live path; this is the poke-it-with-curl view of the same strips.
            return self._json(
                {
                    "ok": True,
                    "plans": {
                        f["pid"]: read_plan(f["slug"]) for f in read_fleet()
                    },
                }
            )
        if path == "/api/dashboards":
            # Tab list (slug/title/order/channels) — the SSE `dashboards` message
            # is the live path; this is the poke-it-with-curl view of the same rows.
            return self._json({"ok": True, "dashboards": dashboards.list_dashboards()})
        if path.startswith("/api/dashboards/"):
            return self._dashboard(path[len("/api/dashboards/"):])
        if path == "/api/asset":
            return self._asset()
        if path == "/api/elevenlabs-key":
            # Masked status only — the full key never reaches the browser.
            return self._json({"ok": True, **actions.elevenlabs_key_status()})
        return self._static(path)

    def do_POST(self):
        if not self._check_auth():
            return self._json({"ok": False, "error": "unauthorized"}, status=401)
        path = self.path.split("?", 1)[0]
        if path == "/api/stt":
            return self._stt()
        body = self._read_body()
        try:
            if path == "/api/message":
                actions.send_message(
                    int(body["pid"]),
                    str(body["text"]),
                    overclock=body.get("overclock") is True,
                )
                return self._json({"ok": True})
            if path == "/api/interrupt":
                actions.interrupt(int(body["pid"]))
                return self._json({"ok": True})
            if path == "/api/voice-listener":
                # Start/stop the host-mic wake-word listener (voice-in driver).
                # The hub's /proc watch rebroadcasts the proc rows, so the
                # switch reflects the real running state without a push here.
                return self._json({"ok": True, **actions.set_voice_listener(body.get("active") is True)})
            if path == "/api/voice-followup":
                # Arm a wake-free follow-up window on the host-mic listener —
                # called when the PAI's read-aloud reply finishes playing.
                window = body.get("window_s")
                return self._json({
                    "ok": True,
                    **actions.open_voice_followup(float(window) if window is not None else 12.0),
                })
            if path == "/api/setup-remote":
                result = actions.setup_remote()
                return self._json({"ok": True, **result})
            if path == "/api/clone":
                result = actions.clone_pai(str(body["source"]))
                return self._json({"ok": True, **result})
            if path == "/api/delete":
                result = actions.delete_pai(str(body["name"]))
                return self._json({"ok": True, **result})
            if path == "/api/kill":
                result = actions.kill_subagent(str(body["name"]))
                return self._json({"ok": True, **result})
            if path == "/api/approve":
                body_override = body.get("body")
                return self._json({
                    "ok": True,
                    **actions.approve_action(
                        str(body["id"]),
                        body_override=str(body_override) if body_override is not None else None,
                    ),
                })
            if path == "/api/reject":
                return self._json(
                    {"ok": True, **actions.reject_action(str(body["id"]), str(body.get("reason", "")))}
                )
            if path == "/api/send-mode":
                # Persist a tri-state send mode; the hub's etc/ watch rebroadcasts
                # the updated send_capabilities, so we don't push state here.
                result = actions.set_send_mode(str(body["flag"]), str(body["mode"]))
                return self._json({"ok": True, **result})
            if path == "/api/plan":
                # Owner edit of the active PAI's live plan.md (checkbox toggle,
                # step add/remove, raw edit). Empty content deletes the file.
                # The hub's /proc watch rebroadcasts the `plan` map, so the
                # optimistic frontend update reconciles without a push here.
                pid = int(body["pid"])
                slug = next((f["slug"] for f in read_fleet() if f["pid"] == pid), None)
                if slug is None:
                    raise ValueError(f"no running PAI with pid {pid}")
                write_plan(slug, str(body.get("content", "")))
                return self._json({"ok": True})
            if path == "/api/shell":
                result = actions.run_shell(int(body["pid"]), str(body["cmd"]))
                return self._json({"ok": True, **result})
            if path == "/api/scheduled":
                # Create an owner scheduled task. The hub's /proc watch
                # rebroadcasts the `scheduled` list, so this doesn't push state.
                result = actions.add_scheduled(
                    str(body["pai"]),
                    str(body["repeat"]),
                    str(body["time"]),
                    dow=body.get("dow"),
                    date=body.get("date"),
                    instruction=str(body.get("instruction", "")),
                )
                return self._json({"ok": True, "task": result})
            if path == "/api/scheduled/update":
                result = actions.update_scheduled(
                    str(body["slug"]),
                    str(body["pai"]),
                    str(body["repeat"]),
                    str(body["time"]),
                    dow=body.get("dow"),
                    date=body.get("date"),
                    instruction=str(body.get("instruction", "")),
                )
                return self._json({"ok": True, "task": result})
            if path == "/api/scheduled/delete":
                return self._json({"ok": True, **actions.remove_scheduled(str(body["slug"]))})
            if path == "/api/models":
                result = actions.set_pai_model(
                    str(body["pai"]), str(body["provider"]), str(body["model"])
                )
                return self._json({"ok": True, **result})
            if path == "/api/apikey":
                result = actions.set_api_key(str(body["provider"]), str(body["key"]))
                return self._json({"ok": True, **result})
            if path == "/api/rename":
                result = actions.set_pai_display_name(
                    str(body["pai"]), str(body["display_name"])
                )
                return self._json({"ok": True, **result})
            if path == "/api/heartbeat":
                hb = body.get("heartbeat")
                result = actions.set_pai_heartbeat(
                    str(body["pai"]), str(hb) if hb is not None else None
                )
                return self._json({"ok": True, **result})
            if path == "/api/kernel":
                action = str(body["action"])
                if action == "start":
                    return self._json({"ok": True, **actions.start_kernel()})
                if action == "stop":
                    return self._json({"ok": True, **actions.stop_kernel()})
                raise ValueError(f"unknown kernel action: {action}")
            if path == "/api/elevenlabs-key":
                # Persist to $PAI_ROOT/.env(.local); goes live on the next TTS
                # request without a restart.
                return self._json({"ok": True, **actions.set_elevenlabs_key(str(body["key"]))})
            if path == "/api/tts":
                voice_id = body.get("voice_id")
                speed = body.get("speed")
                engine = body.get("engine")
                return self._tts(
                    str(body["text"]),
                    voice_id=str(voice_id) if voice_id else None,
                    speed=float(speed) if speed is not None else None,
                    engine=str(engine) if engine else None,
                )
        except (KeyError, ValueError) as e:
            return self._json({"ok": False, "error": str(e)}, status=400)
        except Exception as e:  # noqa: BLE001
            return self._json({"ok": False, "error": str(e)}, status=500)
        return self._json({"ok": False, "error": "not found"}, status=404)

    # -- speech-to-text (voice input) --
    def _stt(self):
        """Proxy recorded browser audio to the server-side STT backend."""
        try:
            audio, filename, content_type, fields = self._read_audio_upload()
            text = actions.transcribe_speech(
                audio,
                filename=filename,
                content_type=content_type,
                language=fields.get("language"),
                prompt=fields.get("prompt"),
            )
        except RuntimeError as e:
            status = 400 if "OPENAI_API_KEY" in str(e) else 502
            return self._json({"ok": False, "error": str(e)}, status=status)
        except ValueError as e:
            return self._json({"ok": False, "error": str(e)}, status=400)
        except Exception as e:  # noqa: BLE001 — upstream / network
            return self._json({"ok": False, "error": str(e)}, status=502)
        return self._json({"ok": True, "text": text})

    def _read_audio_upload(self) -> tuple[bytes, str, str, dict[str, str]]:
        content_type = self.headers.get("Content-Type", "")
        if not content_type.startswith("multipart/form-data"):
            raise ValueError("expected multipart/form-data")

        raw = self._read_raw_body(limit=MAX_STT_UPLOAD_BYTES)
        parser = BytesParser(policy=policy.default)
        msg = parser.parsebytes(
            f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode()
            + raw
        )
        audio: bytes | None = None
        filename = "audio.webm"
        audio_content_type = "audio/webm"
        fields: dict[str, str] = {}

        for part in msg.iter_parts():
            if part.get_content_disposition() != "form-data":
                continue
            name = part.get_param("name", header="content-disposition")
            payload = part.get_payload(decode=True) or b""
            if name == "audio":
                audio = payload
                filename = part.get_filename() or filename
                audio_content_type = part.get_content_type() or audio_content_type
            elif isinstance(name, str):
                fields[name] = payload.decode("utf-8", errors="replace").strip()

        if audio is None:
            raise ValueError("missing audio upload")
        if not audio:
            raise ValueError("empty audio")
        return audio, filename, audio_content_type, fields

    # -- text-to-speech (voice mode) --
    def _tts(
        self,
        text: str,
        *,
        voice_id: str | None = None,
        speed: float | None = None,
        engine: str | None = None,
    ):
        """Proxy text to playable audio via actions.synthesize_speech.

        Keeps ElevenLabs credentials server-side. `engine` is the browser's
        Siri/ElevenLabs toggle. With no ElevenLabs key (or engine="siri"),
        synthesize_speech falls back to macOS `say`; an unavailable local voice
        backend is a config problem (400), while upstream/network failures are
        gateway problems (502). The frontend treats both as "fail quietly" so
        the speech queue never wedges.
        """
        text = text.strip()
        if not text:
            return self._json({"ok": False, "error": "empty text"}, status=400)
        try:
            audio = actions.synthesize_speech(
                text, voice_id=voice_id, speed=speed, engine=engine
            )
        except RuntimeError as e:  # missing key / config
            return self._json({"ok": False, "error": str(e)}, status=400)
        except Exception as e:  # noqa: BLE001 — upstream / network
            return self._json({"ok": False, "error": str(e)}, status=502)
        return self._binary(audio.data, audio.content_type)

    # -- SSE --
    def _stream(self):
        sub = Subscriber()
        HUB.add(sub)
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            self._sse_send(HUB.snapshot())
            while True:
                msg = sub.get(timeout=15)
                if msg is None:
                    self.wfile.write(b": ping\n\n")  # heartbeat / disconnect probe
                    self.wfile.flush()
                else:
                    self._sse_send(msg)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            HUB.remove(sub)

    def _sse_send(self, obj: dict) -> None:
        self.wfile.write(b"data: " + json.dumps(obj).encode() + b"\n\n")
        self.wfile.flush()

    def _asset_root(self) -> Path:
        """Directory tree `/api/asset` is allowed to serve files from.

        The fence tracks the surface's trust boundary, which is exactly what
        `auth_token` already encodes:

          - Remote tunnel (`auth_token` set) — the browser is on the public
            internet behind only a bearer token, so it stays fenced hard to
            PAI_ROOT. Screenshots and downloads a PAI parks under `~/.pai`
            still render; nothing outside it is reachable.
          - Local owner surface (`auth_token is None` — unix socket, or dev
            `pai start --web`) — the owner is viewing their own machine in
            their own browser, so `/api/asset` may reach anything under their
            home dir. That's where PAIs naturally reference files to show:
            `~/Downloads`, `~/Desktop`, screenshots outside `~/.pai`.
        """
        if getattr(getattr(self, "server", None), "auth_token", None):
            return PAI_ROOT.resolve()
        return Path.home().resolve()

    # -- dashboard (PAI-authored HTML, framed in a sandboxed iframe) --
    def _dashboard(self, raw_slug: str):
        """Serve one PAI-authored dashboard's raw HTML for the sandboxed iframe.

        PAIs write arbitrary HTML+JS to `/var/lib/dashboards/<slug>.html`; this
        route hands it to the frontend, which frames it with
        `sandbox="allow-scripts"` (opaque origin — no access to the console's
        session, cookies, or localStorage). The response is walled off hard so
        the PAI's markup can never touch the owner surface:

          - a strict CSP: `default-src 'none'` blocks every network fetch (so
            even inlined JS can't phone out), `script-src`/`style-src
            'unsafe-inline'` allow the dashboard's own inlined code to run, and
            `img-src`/`font-src data:` allow inlined assets. `sandbox
            allow-scripts` re-imposes the opaque origin at the document level, so
            a direct top-level navigation to this URL is sandboxed too — not just
            the iframe. `frame-ancestors 'self'` lets only the console frame it.
          - no wildcard CORS header (see `_binary(cors=False)`), so no
            cross-origin page can read a dashboard out of the loopback server.

        Auth is enforced upstream (`/api/*` requires the token on the remote
        tunnel; the iframe passes it as `?token=` since a document src can't set
        an Authorization header). An unknown/invalid slug is a flat 404.
        """
        slug = raw_slug.split("?", 1)[0].split("#", 1)[0]
        try:
            slug = urllib.parse.unquote(slug)
        except Exception:  # noqa: BLE001
            return self._json({"error": "not found"}, status=404)
        html = dashboards.read_dashboard(slug)
        if html is None:
            return self._json({"error": "not found"}, status=404)
        body = html.encode("utf-8")
        csp = (
            "default-src 'none'; "
            "script-src 'unsafe-inline'; "
            "style-src 'unsafe-inline'; "
            "img-src data:; "
            "font-src data:; "
            "frame-ancestors 'self'; "
            "sandbox allow-scripts"
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Security-Policy", csp)
        # A PAI can rewrite a dashboard anytime; never let a stale copy stick.
        self.send_header("Cache-Control", "no-cache, must-revalidate")
        self.end_headers()
        self.wfile.write(body)

    # -- asset (PAI-referenced files: screenshots, downloads, attachments) --
    def _asset(self):
        """Serve a file addressed by absolute path via `?abs=<path>`.

        PAIs reference files by their absolute on-disk path (e.g.
        `![logo](/Users/…/Downloads/logo.png)`); the frontend rewrites such
        refs to `/api/asset?abs=…&token=…`. This is the only route that reaches
        files the SPA didn't ship, so it is fenced hard:

          - the path must resolve *inside* the surface's asset root (see
            `_asset_root`; symlinks are resolved first so an escape is caught),
          - it must be a real file,
          - a known image extension is served as an image; anything else is
            tried as bounded UTF-8 text, always `text/plain`.

        Anything else is a flat 404 — no directory listing, no error detail.
        Auth is enforced upstream: this is an `/api/*` path, so on the remote
        tunnel `_check_auth` requires the token (passed as `?token=`, since
        `<img>` tags can't set an Authorization header). The response omits the
        wildcard CORS header (see `_binary`) so no cross-origin site can read a
        file out of the loopback server.
        """
        query = self.path.partition("?")[2]
        abs_vals = urllib.parse.parse_qs(query).get("abs")
        if not abs_vals:
            return self._json({"error": "missing abs"}, status=400)
        try:
            target = Path(abs_vals[0]).resolve()
            root = self._asset_root()
        except (OSError, ValueError):
            return self._json({"error": "not found"}, status=404)
        if not target.is_relative_to(root):
            return self._json({"error": "not found"}, status=404)
        if not target.is_file():
            return self._json({"error": "not found"}, status=404)
        ctype = _ASSET_TYPES.get(target.suffix.lower())
        if ctype is not None:
            return self._binary(target.read_bytes(), ctype, cors=False)
        # Not a known image: try to serve it as inline text so the console can
        # render attached files (posts, logs, code) the same way it renders
        # screenshots. Read a bounded prefix and require it to be valid UTF-8;
        # anything binary (or too large to be text) falls through to 404. Always
        # served as text/plain so a `.html`/`.svg` attachment can't execute.
        try:
            raw = target.read_bytes()[: _MAX_TEXT_ASSET_BYTES + 1]
            text = raw.decode("utf-8")
        except (OSError, UnicodeDecodeError):
            return self._json({"error": "not found"}, status=404)
        truncated = len(raw) > _MAX_TEXT_ASSET_BYTES
        if truncated:
            text = text[:_MAX_TEXT_ASSET_BYTES] + "\n… (truncated)"
        return self._binary(text.encode("utf-8"), "text/plain; charset=utf-8", cors=False)

    # -- static (SPA) --
    def _static(self, path: str):
        rel = path.lstrip("/") or "index.html"
        target = (FRONTEND_DIST / rel).resolve()
        is_asset = str(target).startswith(str(FRONTEND_DIST / "assets")) and target.is_file()
        if not str(target).startswith(str(FRONTEND_DIST)) or not target.is_file():
            target = FRONTEND_DIST / "index.html"  # SPA fallback
            is_asset = False
        if not target.is_file():
            return self._json(
                {"error": "frontend not built — run `pnpm build` in frontend/"},
                status=404,
            )
        body = target.read_bytes()
        self.send_response(200)
        self.send_header(
            "Content-Type", _CONTENT_TYPES.get(target.suffix, "application/octet-stream")
        )
        self.send_header("Content-Length", str(len(body)))
        # Cache policy: Vite fingerprints everything under /assets/ by content
        # hash, so those files are immutable — cache them hard. The entry HTML
        # (and the SPA fallback) is NOT fingerprinted and points at the current
        # asset hashes, so it must revalidate every load; otherwise a browser
        # keeps serving a stale index that references a deleted bundle and the
        # console never picks up a new build (this is what hid the Cowork
        # toggle after a deploy).
        if is_asset:
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        else:
            self.send_header("Cache-Control", "no-cache, must-revalidate")
        self.end_headers()
        self.wfile.write(body)


class ThreadingHTTPServer(_StdlibThreadingHTTPServer):
    """ThreadingHTTPServer that doesn't dump a traceback when a client hangs up.

    Browsers routinely reset a connection mid-request (closed tab, aborted
    fetch, HTTP/1.1 keep-alive teardown). The default `handle_error` prints the
    full stack trace to stderr, which buries real errors. Swallow the connection
    teardown family quietly; let anything genuinely unexpected fall through.
    """

    def handle_error(self, request, client_address) -> None:
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionError, BrokenPipeError, TimeoutError)):
            return
        super().handle_error(request, client_address)


class ThreadingUnixHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer over AF_UNIX, for the in-app `pai://` surface.

    BaseHTTPServer is AF_INET by default; flip the address_family and let it
    bind the socket path like a regular `(host, port)` tuple. `server_bind`
    sets server_name/server_port for logging (which we silence anyway).
    """

    address_family = socket.AF_UNIX

    def server_bind(self) -> None:
        # Hardcode a sensible (host, port) for logging since unix sockets
        # don't have either. Skip the AF_INET-specific getsockname unpack.
        socket.socket.bind(self.socket, self.server_address)
        self.server_name = "unix"
        self.server_port = 0


def _bind_unix(server_address: str) -> ThreadingUnixHTTPServer:
    path = Path(server_address)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Stale socket from a previous (crashed) run blocks bind with EADDRINUSE.
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    server = ThreadingUnixHTTPServer(str(path), Handler)
    server.daemon_threads = True
    # Owner-only: the socket is in $PAI_ROOT/run, but be defensive.
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return server


def run(
    host: str = "127.0.0.1",
    port: int = 8787,
    open_browser: bool = False,
    unix_socket: str | None = None,
    auth_token: str | None = None,
    console_restart: Callable[[], None] | None = None,
) -> None:
    """Attach to the running kernel and serve the web surface (blocking).

    Called by `pai start --web` (TCP), by `python -m usr.libexec.web.pai_web`,
    and by PAI.app's WebServerLauncher (unix-socket mode). When `unix_socket`
    is set, `host`/`port`/`open_browser` are ignored.

    `auth_token` is set only by PAI.app's remote (TCP, ngrok-tunneled) instance,
    which puts `/api/*` on the public internet; the local unix-socket surface
    and dev runs leave it `None`. The token is stashed on the server so each
    Handler can read it via `self.server.auth_token` (see `_check_auth`).

    `console_restart` is the caller's way to re-exec this whole process when
    the hub detects the *console* is the stale side of a build skew — after
    `pai update` swaps the release dir, this process keeps serving the old
    `pai_web` code with paths into the wiped dir until it is replaced. The
    listening socket is CLOEXEC, so the fresh image rebinds cleanly.
    """
    HUB.console_restart = console_restart
    HUB.start()
    if unix_socket:
        server = _bind_unix(unix_socket)
        descriptor = f"unix:{unix_socket}"
        url = None
    else:
        server = ThreadingHTTPServer((host, port), Handler)
        server.daemon_threads = True
        url = f"http://{host}:{port}"
        descriptor = url
    server.auth_token = auth_token
    print(f"PAI web surface attached → {descriptor}", file=sys.stderr)
    if not (FRONTEND_DIST / "index.html").is_file():
        print(
            "  (frontend not built yet — run `pnpm install && pnpm build` in "
            f"{FRONTEND_DIST.parent})",
            file=sys.stderr,
        )
    if open_browser and url is not None:
        import threading
        import webbrowser

        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        HUB.stop()
        server.shutdown()
        if unix_socket:
            try:
                Path(unix_socket).unlink()
            except FileNotFoundError:
                pass


def main() -> None:
    parser = argparse.ArgumentParser(prog="pai-web", description="PAI web surface")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--open", action="store_true", help="open a browser tab")
    parser.add_argument(
        "--unix-socket",
        default=None,
        help="bind a unix-domain socket at this path instead of TCP",
    )
    parser.add_argument(
        "--auth-token",
        default=None,
        help="require this bearer token on /api/* (remote/tunneled TCP only)",
    )
    args = parser.parse_args()
    # Self re-exec for build-skew healing: same serving config, fresh code.
    # Module form through the stable interpreter/`usr/src` paths, so the new
    # image loads the freshly-installed release. Never re-adds --open — the
    # owner's tab reconnects over SSE on its own.
    reexec = [sys.executable, "-m", "usr.libexec.web.pai_web",
              "--host", args.host, "--port", str(args.port)]
    if args.unix_socket:
        reexec += ["--unix-socket", args.unix_socket]
    if args.auth_token:
        reexec += ["--auth-token", args.auth_token]
    run(
        host=args.host,
        port=args.port,
        open_browser=args.open,
        unix_socket=args.unix_socket,
        auth_token=args.auth_token,
        console_restart=lambda: os.execv(sys.executable, reexec),
    )


if __name__ == "__main__":
    main()
