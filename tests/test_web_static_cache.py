"""The static (SPA) handler's cache policy.

Vite fingerprints everything under /assets/ by content hash, so those files
are immutable and cache hard. The entry HTML is NOT fingerprinted and points
at the current asset hashes, so it must revalidate every load — otherwise a
browser keeps serving a stale index that references a deleted bundle and the
console never picks up a new build (this hid the Cowork toggle after a deploy).
"""
from __future__ import annotations

import io
import types

import pytest

from usr.libexec.web.pai_web import server


def _serve(monkeypatch, tmp_path, req_path: str) -> dict[str, str]:
    """Drive Handler._static for req_path against a fake dist; return the
    response headers as a dict."""
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><html></html>")
    (dist / "assets" / "index-abc123.js").write_text("console.log(1)")
    monkeypatch.setattr(server, "FRONTEND_DIST", dist, raising=True)

    handler = server.Handler.__new__(server.Handler)
    handler.wfile = io.BytesIO()
    headers: dict[str, str] = {}
    status = {}
    handler.send_response = lambda code, *a: status.setdefault("code", code)
    handler.send_header = lambda k, v: headers.__setitem__(k, v)
    handler.end_headers = lambda: None

    handler._static(req_path)
    headers["__status__"] = status.get("code")
    return headers


def test_index_html_must_revalidate(monkeypatch, tmp_path):
    h = _serve(monkeypatch, tmp_path, "/")
    assert h["__status__"] == 200
    assert h["Cache-Control"] == "no-cache, must-revalidate"


def test_spa_fallback_is_no_cache(monkeypatch, tmp_path):
    # An unknown client-route path falls back to index.html — same policy, so a
    # deep link doesn't pin a stale shell either.
    h = _serve(monkeypatch, tmp_path, "/some/deep/route")
    assert h["Cache-Control"] == "no-cache, must-revalidate"


def test_hashed_asset_is_immutable(monkeypatch, tmp_path):
    h = _serve(monkeypatch, tmp_path, "/assets/index-abc123.js")
    assert h["__status__"] == 200
    assert h["Cache-Control"] == "public, max-age=31536000, immutable"
