"""pai-mail-oauth — OAuth2 loopback-flow helper for IMAP/SMTP XOAUTH2.

Three subcommands:

    pai-mail-oauth setup <address> --provider <gmail|outlook>
        Run the loopback consent flow. Spins up a localhost HTTP server
        on a random port, opens the user's browser to the IdP's consent
        page with redirect_uri=http://127.0.0.1:<port>/callback, catches
        the redirect, and exchanges the auth code for a refresh token.
        Stores the long-lived refresh token at:

            /etc/secrets/mail/<address>/refresh_token
            /etc/secrets/mail/<address>/oauth_provider   (google|microsoft)

    pai-mail-oauth get <address>
        Print a fresh access token to stdout. Used as `PassCmd` /
        `passwordeval` from mbsync and msmtp. Reads the cache at
        sys/drivers/maildir/oauth/<address>.json and refreshes via the
        stored refresh token when the cached token is near expiry.

    pai-mail-oauth status <address>
        Diagnostic: which provider, refresh token present?, cache TTL.

The helper has no kernel state of its own — every invocation reads files
and exits. mbsync and msmtp call it on every connection.
"""

from __future__ import annotations

import argparse
import http.server
import json
import secrets
import socket
import sys
import time
import urllib.parse
import webbrowser
from pathlib import Path
from typing import Optional

import requests
import yaml

from boot import paths


_PROVIDERS_PATH = Path(__file__).resolve().parent / "providers.yaml"
# Refresh proactively when the cached access token has less than this many
# seconds of life left. Avoids handing out a token that expires mid-IMAP.
_REFRESH_GRACE_SECONDS = 60


# ---------- provider table ------------------------------------------------

def _load_provider_table() -> dict:
    with _PROVIDERS_PATH.open() as f:
        return yaml.safe_load(f) or {}


def _resolve_provider(name: str) -> tuple[str, dict]:
    """Map an account-side `provider:` value to an IdP entry.

    `name` is what the user wrote in accounts.yaml: `gmail`, `outlook`,
    or directly `google`/`microsoft`. Returns (canonical_idp_name, config).
    """
    table = _load_provider_table()
    aliases: dict = table.get("provider_alias") or {}
    canonical = aliases.get(name, name)
    providers = table.get("providers") or {}
    cfg = providers.get(canonical)
    if cfg is None:
        raise SystemExit(
            f"pai-mail-oauth: unknown provider {name!r} "
            f"(known: {', '.join(sorted(providers))})"
        )
    return canonical, cfg


# ---------- on-disk paths -------------------------------------------------

def _secret_dir(address: str) -> Path:
    return paths.etc_secrets() / "mail" / address


def _refresh_token_path(address: str) -> Path:
    return _secret_dir(address) / "refresh_token"


def _provider_marker_path(address: str) -> Path:
    return _secret_dir(address) / "oauth_provider"


def _cache_path(address: str) -> Path:
    return paths.sys_drivers() / "maildir" / "oauth" / f"{address}.json"


def _atomic_write(path: Path, text: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    tmp.chmod(mode)
    tmp.replace(path)


# ---------- loopback flow (setup) -----------------------------------------

def _post_form(url: str, data: dict) -> dict:
    r = requests.post(url, data=data, timeout=30)
    try:
        body = r.json()
    except ValueError:
        raise SystemExit(
            f"pai-mail-oauth: non-JSON response from {url} "
            f"(status {r.status_code}): {r.text[:200]}"
        )
    return body


def _pick_port() -> int:
    """Bind to 127.0.0.1:0, capture the OS-assigned port, release. The
    HTTP server we start next will rebind to it.

    Race window between release and rebind is a few ms; in practice nothing
    else will grab the port.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """One-shot handler: captures the query string of the first GET, writes
    a friendly HTML response, then signals the server to shut down."""

    captured: dict | None = None

    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return
        type(self).captured = dict(urllib.parse.parse_qsl(parsed.query))
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        body = (
            "<html><body style='font-family:sans-serif;padding:2em'>"
            "<h2>Authorization received.</h2>"
            "<p>You can close this tab and return to the terminal.</p>"
            "</body></html>"
        )
        self.wfile.write(body.encode("utf-8"))

    def log_message(self, *_args, **_kwargs) -> None:  # noqa: N802
        # Suppress default access-log spam on stdout.
        return


def _wait_for_callback(port: int, timeout_seconds: int = 300) -> dict:
    """Run the one-shot HTTP server until /callback fires (or timeout)."""
    server = http.server.HTTPServer(("127.0.0.1", port), _CallbackHandler)
    server.timeout = 1
    deadline = time.time() + timeout_seconds
    _CallbackHandler.captured = None
    try:
        while _CallbackHandler.captured is None and time.time() < deadline:
            server.handle_request()
    finally:
        server.server_close()
    if _CallbackHandler.captured is None:
        raise SystemExit("pai-mail-oauth: timed out waiting for browser redirect")
    return _CallbackHandler.captured


def cmd_setup(args: argparse.Namespace) -> int:
    address: str = args.address
    canonical, cfg = _resolve_provider(args.provider)

    scope = " ".join(cfg.get("scopes") or [])
    auth_endpoint = cfg["auth_endpoint"]
    token_endpoint = cfg["token_endpoint"]
    client_id = cfg["client_id"]
    client_secret = cfg.get("client_secret") or ""
    extra = dict(cfg.get("extra_auth_params") or {})

    port = _pick_port()
    redirect_uri = f"http://127.0.0.1:{port}/callback"
    state = secrets.token_urlsafe(24)

    auth_params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": scope,
        "state": state,
        "login_hint": address,
        **extra,
    }
    auth_url = f"{auth_endpoint}?{urllib.parse.urlencode(auth_params)}"

    print()
    print(f"  Opening browser for {address} ({canonical})...")
    print(f"  If it doesn't open automatically, visit:")
    print(f"    {auth_url}")
    print()
    print(f"  Listening on {redirect_uri}")
    print()

    try:
        webbrowser.open(auth_url)
    except webbrowser.Error:
        pass

    captured = _wait_for_callback(port)

    if captured.get("state") != state:
        raise SystemExit("pai-mail-oauth: state mismatch on callback (possible CSRF)")
    if "error" in captured:
        raise SystemExit(
            f"pai-mail-oauth: consent denied: {captured['error']}: "
            f"{captured.get('error_description', '')}"
        )
    code = captured.get("code")
    if not code:
        raise SystemExit("pai-mail-oauth: callback missing ?code=")

    body = _post_form(token_endpoint, {
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    })
    err = body.get("error")
    if err:
        raise SystemExit(
            f"pai-mail-oauth: token exchange failed: {err}: "
            f"{body.get('error_description')}"
        )

    refresh = body.get("refresh_token")
    access = body.get("access_token")
    if not refresh:
        raise SystemExit(
            "pai-mail-oauth: server returned no refresh_token; check that "
            "the requested scopes include offline_access (Microsoft) and "
            "that prompt=consent is set (Google)."
        )
    _atomic_write(_refresh_token_path(address), refresh)
    _atomic_write(_provider_marker_path(address), canonical + "\n")
    if access:
        _save_cache(address, access, int(body.get("expires_in", 3600)))
    print(f"  ✓ Refresh token stored at {_refresh_token_path(address)}")
    print(f"  ✓ Provider:               {canonical}")
    print()
    return 0


# ---------- access-token retrieval ----------------------------------------

def _load_cache(address: str) -> Optional[dict]:
    path = _cache_path(address)
    if not path.exists():
        return None
    try:
        with path.open() as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _save_cache(address: str, access_token: str, expires_in: int) -> None:
    expires_at = int(time.time()) + max(0, int(expires_in) - _REFRESH_GRACE_SECONDS)
    body = json.dumps({
        "access_token": access_token,
        "expires_at": expires_at,
    })
    _atomic_write(_cache_path(address), body, mode=0o600)


def _refresh_access_token(address: str) -> str:
    """Exchange the stored refresh token for a fresh access token. Updates
    the cache and returns the new access token.
    """
    refresh_path = _refresh_token_path(address)
    if not refresh_path.exists():
        raise SystemExit(
            f"pai-mail-oauth: no refresh token for {address!r}; "
            f"run `pai-mail-oauth setup {address} --provider <gmail|outlook>` first"
        )
    provider_path = _provider_marker_path(address)
    if not provider_path.exists():
        raise SystemExit(
            f"pai-mail-oauth: missing provider marker {provider_path}; "
            f"re-run `pai-mail-oauth setup {address} ...`"
        )
    canonical = provider_path.read_text().strip()
    _, cfg = _resolve_provider(canonical)

    body = _post_form(cfg["token_endpoint"], {
        "client_id": cfg["client_id"],
        "client_secret": cfg.get("client_secret") or "",
        "refresh_token": refresh_path.read_text().strip(),
        "grant_type": "refresh_token",
    })
    err = body.get("error")
    if err:
        raise SystemExit(
            f"pai-mail-oauth: refresh failed: {err}: "
            f"{body.get('error_description')}"
        )
    access = body.get("access_token")
    if not access:
        raise SystemExit("pai-mail-oauth: refresh returned no access_token")
    _save_cache(address, access, int(body.get("expires_in", 3600)))
    return access


def cmd_get(args: argparse.Namespace) -> int:
    address: str = args.address
    cache = _load_cache(address)
    if cache and int(cache.get("expires_at", 0)) > int(time.time()):
        sys.stdout.write(cache["access_token"])
        sys.stdout.flush()
        return 0
    token = _refresh_access_token(address)
    sys.stdout.write(token)
    sys.stdout.flush()
    return 0


# ---------- diagnostics ---------------------------------------------------

def cmd_status(args: argparse.Namespace) -> int:
    address: str = args.address
    refresh_path = _refresh_token_path(address)
    provider_path = _provider_marker_path(address)
    cache = _load_cache(address)
    print(f"address:        {address}")
    print(f"provider:       "
          f"{provider_path.read_text().strip() if provider_path.exists() else '(not configured)'}")
    print(f"refresh token:  {'present' if refresh_path.exists() else 'MISSING'}")
    if cache:
        ttl = int(cache.get("expires_at", 0)) - int(time.time())
        print(f"access cache:   {ttl}s remaining (at {_cache_path(address)})")
    else:
        print(f"access cache:   empty")
    return 0


# ---------- entrypoint ----------------------------------------------------

def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="pai-mail-oauth", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_setup = sub.add_parser("setup", help="run OAuth2 device-code consent")
    p_setup.add_argument("address", help="email address (e.g., me@gmail.com)")
    p_setup.add_argument(
        "--provider",
        required=True,
        help="gmail | outlook (or canonical: google | microsoft)",
    )
    p_setup.set_defaults(func=cmd_setup)

    p_get = sub.add_parser("get", help="print a fresh access token to stdout")
    p_get.add_argument("address")
    p_get.set_defaults(func=cmd_get)

    p_status = sub.add_parser("status", help="show OAuth state for an account")
    p_status.add_argument("address")
    p_status.set_defaults(func=cmd_status)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
