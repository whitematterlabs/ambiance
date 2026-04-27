"""Thin Gmail REST wrapper using `requests` + bearer token.

Avoids `google-api-python-client` (~10MB of deps + discovery cache) — we
hit only three endpoints. Each call refreshes creds if needed and raises
`HistoryIdInvalid` on the well-known 404 path.
"""

from __future__ import annotations

from typing import Optional

import requests
from google.oauth2.credentials import Credentials

API = "https://gmail.googleapis.com/gmail/v1/users/me"


class HistoryIdInvalid(Exception):
    """Raised on `404 historyId not found` — caller must rebootstrap."""


class GmailApiError(Exception):
    pass


def _headers(creds: Credentials) -> dict:
    return {"Authorization": f"Bearer {creds.token}"}


def _get(creds: Credentials, url: str, params: Optional[dict] = None) -> dict:
    r = requests.get(url, headers=_headers(creds), params=params, timeout=30)
    if r.status_code == 404 and "historyId" in r.text:
        raise HistoryIdInvalid(r.text)
    if not r.ok:
        raise GmailApiError(f"GET {url} -> {r.status_code}: {r.text[:500]}")
    return r.json()


def get_profile(creds: Credentials) -> dict:
    return _get(creds, f"{API}/profile")


def history_list(
    creds: Credentials,
    start_history_id: str,
    page_token: Optional[str] = None,
) -> dict:
    params = {
        "startHistoryId": start_history_id,
        "historyTypes": "messageAdded",
        "labelId": "INBOX",
    }
    if page_token:
        params["pageToken"] = page_token
    return _get(creds, f"{API}/history", params=params)


def messages_get(creds: Credentials, message_id: str) -> dict:
    return _get(creds, f"{API}/messages/{message_id}", params={"format": "full"})
