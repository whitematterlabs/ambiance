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

from boot.paths import REPO_ROOT, usr_libexec

from . import actions
from .hub import Hub, Subscriber


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

    def _binary(self, data: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
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
        if path == "/api/state":
            return self._json(HUB.snapshot(actions.read_provider()))
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
                actions.send_message(int(body["pid"]), str(body["text"]))
                return self._json({"ok": True})
            if path == "/api/interrupt":
                actions.interrupt(int(body["pid"]))
                return self._json({"ok": True})
            if path == "/api/clone":
                result = actions.clone_pai(str(body["source"]))
                return self._json({"ok": True, **result})
            if path == "/api/delete":
                result = actions.delete_pai(str(body["name"]))
                return self._json({"ok": True, **result})
            if path == "/api/shell":
                result = actions.run_shell(int(body["pid"]), str(body["cmd"]))
                return self._json({"ok": True, **result})
            if path == "/api/provider":
                key = actions.write_provider(str(body["key"]))
                HUB._broadcast({"type": "provider", "provider": key})
                return self._json({"ok": True, "provider": key})
            if path == "/api/kernel":
                action = str(body["action"])
                if action == "start":
                    return self._json({"ok": True, **actions.start_kernel()})
                if action == "stop":
                    return self._json({"ok": True, **actions.stop_kernel()})
                raise ValueError(f"unknown kernel action: {action}")
            if path == "/api/tts":
                voice_id = body.get("voice_id")
                speed = body.get("speed")
                return self._tts(
                    str(body["text"]),
                    voice_id=str(voice_id) if voice_id else None,
                    speed=float(speed) if speed is not None else None,
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
    def _tts(self, text: str, *, voice_id: str | None = None, speed: float | None = None):
        """Proxy text to playable audio via actions.synthesize_speech.

        Keeps ElevenLabs credentials server-side. With no ElevenLabs key,
        synthesize_speech falls back to macOS `say`; an unavailable local voice
        backend is a config problem (400), while upstream/network failures are
        gateway problems (502). The frontend treats both as "fail quietly" so
        the speech queue never wedges.
        """
        text = text.strip()
        if not text:
            return self._json({"ok": False, "error": "empty text"}, status=400)
        try:
            audio = actions.synthesize_speech(text, voice_id=voice_id, speed=speed)
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
            self._sse_send(HUB.snapshot(actions.read_provider()))
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

    # -- static (SPA) --
    def _static(self, path: str):
        rel = path.lstrip("/") or "index.html"
        target = (FRONTEND_DIST / rel).resolve()
        if not str(target).startswith(str(FRONTEND_DIST)) or not target.is_file():
            target = FRONTEND_DIST / "index.html"  # SPA fallback
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
) -> None:
    """Attach to the running kernel and serve the web surface (blocking).

    Called by `pai start --web` (TCP), by `python -m usr.libexec.web.pai_web`,
    and by PAI.app's WebServerLauncher (unix-socket mode). When `unix_socket`
    is set, `host`/`port`/`open_browser` are ignored.

    `auth_token` is set only by PAI.app's remote (TCP, ngrok-tunneled) instance,
    which puts `/api/*` on the public internet; the local unix-socket surface
    and dev runs leave it `None`. The token is stashed on the server so each
    Handler can read it via `self.server.auth_token` (see `_check_auth`).
    """
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
    run(
        host=args.host,
        port=args.port,
        open_browser=args.open,
        unix_socket=args.unix_socket,
        auth_token=args.auth_token,
    )


if __name__ == "__main__":
    main()
