"""The dashboard-serving route's security headers.

A dashboard is PAI-authored HTML+JS. The console frames it in a sandboxed
iframe, but the server-side response is the real trust boundary: a strict CSP
(`default-src 'none'` + a document-level `sandbox`) so the markup can never
phone out or touch the owner surface even on a direct top-level navigation, and
no wildcard CORS so no cross-origin site can read a dashboard out of the
loopback server. An unknown/traversal slug is a flat 404.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from boot import paths as PA
from usr.libexec.web.pai_web import server


def _serve_dashboard(monkeypatch, tmp_path: Path, raw_slug: str) -> dict:
    """Drive Handler._dashboard for raw_slug; return headers + body + status."""
    monkeypatch.setattr(PA, "PAI_ROOT", tmp_path, raising=True)
    root = tmp_path / "var" / "lib" / "dashboards"
    root.mkdir(parents=True, exist_ok=True)
    (root / "sales.html").write_text(
        '<script type="application/pai-dashboard+json">{"title":"Sales"}</script>'
        "<!doctype html><h1>Sales</h1>",
        encoding="utf-8",
    )

    handler = server.Handler.__new__(server.Handler)
    handler.wfile = io.BytesIO()
    headers: dict[str, str] = {}
    status: dict[str, int] = {}
    handler.send_response = lambda code, *a: status.setdefault("code", code)
    handler.send_header = lambda k, v: headers.__setitem__(k, v)
    handler.end_headers = lambda: None
    # _json (the 404 path) uses these too.
    handler._dashboard(raw_slug)

    return {
        "status": status.get("code"),
        "headers": headers,
        "body": handler.wfile.getvalue(),
    }


def test_serves_html_with_strict_csp(monkeypatch, tmp_path):
    r = _serve_dashboard(monkeypatch, tmp_path, "sales")
    assert r["status"] == 200
    assert r["headers"]["Content-Type"] == "text/html; charset=utf-8"
    assert b"<h1>Sales</h1>" in r["body"]
    csp = r["headers"]["Content-Security-Policy"]
    assert "default-src 'none'" in csp
    assert "sandbox allow-scripts" in csp
    assert "frame-ancestors 'self'" in csp


def test_no_wildcard_cors(monkeypatch, tmp_path):
    r = _serve_dashboard(monkeypatch, tmp_path, "sales")
    # A cross-origin page must not be able to read a dashboard out of loopback.
    assert "Access-Control-Allow-Origin" not in r["headers"]


def test_strips_query_and_fragment(monkeypatch, tmp_path):
    # The iframe src carries `?token=…`; the slug must resolve without it.
    r = _serve_dashboard(monkeypatch, tmp_path, "sales?token=abc#frag")
    assert r["status"] == 200
    assert b"<h1>Sales</h1>" in r["body"]


def test_missing_slug_404(monkeypatch, tmp_path):
    r = _serve_dashboard(monkeypatch, tmp_path, "ghost")
    assert r["status"] == 404


def test_traversal_slug_404(monkeypatch, tmp_path):
    r = _serve_dashboard(monkeypatch, tmp_path, "../../etc/passwd")
    assert r["status"] == 404
