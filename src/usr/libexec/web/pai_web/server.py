"""Stdlib HTTP + SSE server for the PAI web surface.

No third-party web framework: `http.server.ThreadingHTTPServer` gives one
thread per request, which is all an SSE stream needs (it blocks on its mailbox).
The browser→kernel direction is plain POST; the kernel→browser direction is a
single long-lived Server-Sent Events stream fed by `hub.Hub`.

Run:  python -m usr.libexec.web.pai_web            # serves built frontend at http://127.0.0.1:8787
      python -m usr.libexec.web.pai_web --port N   # custom port
In dev, run Vite (`pnpm dev`) and let it proxy /api to this server.
"""

from __future__ import annotations

import argparse
from email import policy
from email.parser import BytesParser
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from boot.paths import REPO_ROOT, usr_libexec

from . import actions
from .hub import Hub, Subscriber


def _frontend_dist() -> Path:
    """Where the built React frontend lives.

    The web surface *attaches* to the kernel from outside; it does not live in
    the kernel's runtime root (~/.pai). So it resolves its own assets: from the
    repo's libexec sidecar slot in dev, or from an embedded slot if a shipped
    app populated `usr/libexec/web/`. Nothing is injected into ~/.pai.
    """
    candidates = (
        REPO_ROOT / "src" / "usr" / "libexec" / "web" / "dist",  # dev: read from repo
        usr_libexec() / "web" / "dist",                          # shipped: embedded slot
    )
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

    # -- routing --
    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
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
                return self._tts(str(body["text"]))
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
    def _tts(self, text: str):
        """Proxy text → mp3 bytes via actions.synthesize_speech (ElevenLabs).

        Keeps the API key server-side. A missing key is a config problem (400);
        an upstream/network failure is a gateway problem (502). The frontend
        treats both as "fail quietly" so the speech queue never wedges.
        """
        text = text.strip()
        if not text:
            return self._json({"ok": False, "error": "empty text"}, status=400)
        try:
            audio = actions.synthesize_speech(text)
        except RuntimeError as e:  # missing key / config
            return self._json({"ok": False, "error": str(e)}, status=400)
        except Exception as e:  # noqa: BLE001 — upstream / network
            return self._json({"ok": False, "error": str(e)}, status=502)
        return self._binary(audio, "audio/mpeg")

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


def run(host: str = "127.0.0.1", port: int = 8787, open_browser: bool = False) -> None:
    """Attach to the running kernel and serve the web surface (blocking).

    Called by `pai start --web` and by `python -m usr.libexec.web.pai_web`.
    """
    HUB.start()
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    url = f"http://{host}:{port}"
    print(f"PAI web surface attached → {url}", file=sys.stderr)
    if not (FRONTEND_DIST / "index.html").is_file():
        print(
            "  (frontend not built yet — run `pnpm install && pnpm build` in "
            f"{FRONTEND_DIST.parent})",
            file=sys.stderr,
        )
    if open_browser:
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


def main() -> None:
    parser = argparse.ArgumentParser(prog="pai-web", description="PAI web surface")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--open", action="store_true", help="open a browser tab")
    args = parser.parse_args()
    run(host=args.host, port=args.port, open_browser=args.open)


if __name__ == "__main__":
    main()
