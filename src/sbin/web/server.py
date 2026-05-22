"""Stdlib HTTP + SSE server for the PAI web surface.

No third-party web framework: `http.server.ThreadingHTTPServer` gives one
thread per request, which is all an SSE stream needs (it blocks on its mailbox).
The browser→kernel direction is plain POST; the kernel→browser direction is a
single long-lived Server-Sent Events stream fed by `hub.Hub`.

Run:  paiweb            # serves built frontend at http://127.0.0.1:8787
      paiweb --port N   # custom port
In dev, run Vite (`pnpm dev`) and let it proxy /api to this server.
"""

from __future__ import annotations

import argparse
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from boot.paths import usr_libexec

from . import actions
from .hub import Hub, Subscriber

# The React frontend is a non-Python sidecar: its source + node_modules + build
# live in the FHS sidecar slot (usr/libexec/web/), not next to this Python.
# paifs-init symlinks usr/libexec/web at the live repo for dev.
FRONTEND_DIST = usr_libexec() / "web" / "dist"

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

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0) or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return {}

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
        if path == "/api/state":
            return self._json(HUB.snapshot(actions.read_provider()))
        return self._static(path)

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        body = self._read_body()
        try:
            if path == "/api/message":
                actions.send_message(int(body["pid"]), str(body["text"]))
                return self._json({"ok": True})
            if path == "/api/interrupt":
                actions.interrupt(int(body["pid"]))
                return self._json({"ok": True})
            if path == "/api/shell":
                result = actions.run_shell(int(body["pid"]), str(body["cmd"]))
                return self._json({"ok": True, **result})
            if path == "/api/provider":
                key = actions.write_provider(str(body["key"]))
                HUB._broadcast({"type": "provider", "provider": key})
                return self._json({"ok": True, "provider": key})
        except (KeyError, ValueError) as e:
            return self._json({"ok": False, "error": str(e)}, status=400)
        except Exception as e:  # noqa: BLE001
            return self._json({"ok": False, "error": str(e)}, status=500)
        return self._json({"ok": False, "error": "not found"}, status=404)

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

    Called by `pai start --web` and by `python -m sbin.web`.
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
