"""Gmail OAuth — installed-app loopback flow + refresh-token persistence.

Personal Gmail accounts use Google's "Desktop app" OAuth client. The client
secret is non-confidential for installed apps (Google's docs are explicit
about this), so storing it under home/ alongside the refresh token is fine.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
]


def _client_config(client_id: str, client_secret: str) -> dict:
    return {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }


def loopback_oauth(client_id: str, client_secret: str) -> Credentials:
    """Run the loopback OAuth flow. Opens a browser; blocks until callback."""
    flow = InstalledAppFlow.from_client_config(_client_config(client_id, client_secret), SCOPES)
    return flow.run_local_server(port=0, open_browser=True, prompt="consent")


def save_credentials(creds: Credentials, token_path: Path) -> None:
    token_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": list(creds.scopes or []),
        "expiry": creds.expiry.isoformat() if creds.expiry else None,
    }
    tmp = token_path.with_suffix(token_path.suffix + ".tmp")
    with tmp.open("w") as f:
        json.dump(payload, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, token_path)


def load_credentials(token_path: Path) -> Credentials:
    """Load creds; refresh + write back if expired."""
    with token_path.open() as f:
        data = json.load(f)
    creds = Credentials(
        token=data.get("token"),
        refresh_token=data.get("refresh_token"),
        token_uri=data.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=data.get("client_id"),
        client_secret=data.get("client_secret"),
        scopes=data.get("scopes") or SCOPES,
    )
    if not creds.valid:
        if creds.refresh_token:
            creds.refresh(Request())
            save_credentials(creds, token_path)
        else:
            raise RuntimeError(f"no refresh_token in {token_path}; re-run addemail")
    return creds
