from __future__ import annotations

import types

import pytest

from usr.libexec.web.pai_web import server


def make_handler(auth_token, path, headers=None):
    """A bare Handler with just enough wired to exercise the auth gate.

    Mirrors test_web_kernel.py's __new__ pattern: no real socket/server, only
    the attributes the auth check reads (self.server.auth_token, self.path,
    self.headers).
    """
    handler = server.Handler.__new__(server.Handler)
    handler.path = path
    handler.server = types.SimpleNamespace(auth_token=auth_token)
    handler.headers = headers or {}
    return handler


def test_no_token_allows_everything() -> None:
    # Local unix-socket / dev `pai start --web`: auth_token is None → unchanged.
    for path in ("/", "/index.html", "/api/state", "/api/stream"):
        assert make_handler(None, path)._check_auth() is True


def test_missing_server_attr_allows() -> None:
    # Existing do_GET/do_POST unit tests build Handler without a .server; the
    # gate must degrade to "allow" rather than AttributeError.
    handler = server.Handler.__new__(server.Handler)
    handler.path = "/api/state"
    handler.headers = {}
    assert handler._check_auth() is True


def test_static_shell_is_exempt_even_with_token() -> None:
    for path in ("/", "/index.html", "/assets/app.js", "/manifest.webmanifest"):
        assert make_handler("secret", path)._check_auth() is True


def test_health_is_exempt_even_with_token() -> None:
    assert make_handler("secret", "/api/health")._check_auth() is True


def test_api_without_token_is_rejected() -> None:
    assert make_handler("secret", "/api/state")._check_auth() is False


def test_api_with_bearer_header_is_allowed() -> None:
    handler = make_handler(
        "secret", "/api/state", headers={"Authorization": "Bearer secret"}
    )
    assert handler._check_auth() is True


def test_api_with_wrong_bearer_is_rejected() -> None:
    handler = make_handler(
        "secret", "/api/state", headers={"Authorization": "Bearer nope"}
    )
    assert handler._check_auth() is False


def test_sse_with_query_token_is_allowed() -> None:
    # EventSource can't set headers, so the SSE stream carries ?token=.
    assert make_handler("secret", "/api/stream?token=secret")._check_auth() is True


def test_query_token_mismatch_is_rejected() -> None:
    assert make_handler("secret", "/api/state?token=wrong")._check_auth() is False


def test_do_get_gate_returns_401(monkeypatch: pytest.MonkeyPatch) -> None:
    # End-to-end through do_GET: an unauthenticated /api/state never reaches
    # the route dispatch — it short-circuits to a 401 JSON body.
    sent: dict = {}
    handler = make_handler("secret", "/api/state")
    handler._json = lambda obj, status=200: sent.update({"obj": obj, "status": status})
    handler._static = lambda path: pytest.fail("static fallback should not run")

    server.Handler.do_GET(handler)

    assert sent == {"obj": {"ok": False, "error": "unauthorized"}, "status": 401}


def test_do_post_gate_returns_401() -> None:
    sent: dict = {}
    handler = make_handler("secret", "/api/shell")
    handler._json = lambda obj, status=200: sent.update({"obj": obj, "status": status})
    handler._read_body = lambda: pytest.fail("body should not be read on 401")

    server.Handler.do_POST(handler)

    assert sent == {"obj": {"ok": False, "error": "unauthorized"}, "status": 401}


def test_run_passes_auth_token_to_server(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    class FakeServer:
        def __init__(self, addr, handler) -> None:
            captured["addr"] = addr
            captured["server"] = self

        def serve_forever(self) -> None:
            raise KeyboardInterrupt

        def shutdown(self) -> None:
            pass

    monkeypatch.setattr(server, "ThreadingHTTPServer", FakeServer)
    monkeypatch.setattr(server.HUB, "start", lambda: None)
    monkeypatch.setattr(server.HUB, "stop", lambda: None)

    server.run(host="127.0.0.1", port=8787, auth_token="tok123")

    # The server instance carries the token so Handler can read it per request.
    assert captured["addr"] == ("127.0.0.1", 8787)
    assert captured["server"].auth_token == "tok123"
