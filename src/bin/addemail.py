#!/usr/bin/env python
"""addemail — onboard a Gmail account for the inbound driver.

Runs the Google installed-app OAuth loopback flow (browser pops up),
captures the current Gmail historyId as the bootstrap cursor (so the
driver starts from "now" and never backfills), and writes:

  home/communication/email/{account}/meta.yaml
  home/communication/email/{account}/{threads,drafts}/   (empty)
  home/tmp/drivers/gmail-in/{account}/token.json
  home/tmp/drivers/gmail-in/{account}/history-id

Usage:
    uv run python src/bin/addemail.py ACCOUNT [--client-id ID] [--poll 60]

`client_id` and `client_secret` come from a Google Cloud "Desktop app"
OAuth credential. Default to $GOOGLE_API_CLIENT_ID and
$GOOGLE_API_CLIENT_SECRET (loaded from .env.local).

Refuses to run if the account already exists. Does NOT talk to the
running kernel — restart the kernel after onboarding.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date
from pathlib import Path

import yaml

from drivers.email.gmail import api as gapi
from drivers.email.gmail import auth as gauth
from boot import processes as P  # noqa: F401  — triggers .env.local load

EMAIL_ROOT = P.HOME_DIR / "communication" / "email"
TMP_ROOT = P.HOME_DIR / "tmp" / "drivers" / "gmail-in"

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _die(msg: str) -> None:
    print(f"addemail: {msg}", file=sys.stderr)
    sys.exit(1)


def main() -> int:
    ap = argparse.ArgumentParser(description="Onboard a Gmail account.")
    ap.add_argument("account", help="Email address, e.g. you@gmail.com")
    ap.add_argument(
        "--client-id",
        default=os.environ.get("GOOGLE_API_CLIENT_ID"),
        help="Google OAuth Desktop app client_id. Defaults to $GOOGLE_API_CLIENT_ID.",
    )
    ap.add_argument(
        "--client-secret",
        default=os.environ.get("GOOGLE_API_CLIENT_SECRET"),
        help="Google OAuth Desktop app client_secret. Defaults to $GOOGLE_API_CLIENT_SECRET.",
    )
    ap.add_argument("--poll", type=int, default=60, help="Poll interval seconds (default 60)")
    args = ap.parse_args()

    account = args.account.strip().lower()
    if not _EMAIL_RE.match(account):
        _die(f"invalid email: {account!r}")
    if not args.client_id:
        _die("no client_id: set GOOGLE_API_CLIENT_ID in .env.local or pass --client-id")
    if not args.client_secret:
        _die("no client_secret: set GOOGLE_API_CLIENT_SECRET in .env.local or pass --client-secret")

    account_dir = EMAIL_ROOT / account
    meta_path = account_dir / "meta.yaml"
    if meta_path.exists():
        _die(f"account already exists: {meta_path}")

    print(f"addemail: starting OAuth flow for {account} (browser will open)…")
    creds = gauth.loopback_oauth(args.client_id, args.client_secret)

    profile = gapi.get_profile(creds)
    profile_email = (profile.get("emailAddress") or "").lower()
    if profile_email and profile_email != account:
        _die(
            f"OAuth returned account {profile_email!r} but you specified "
            f"{account!r}. Re-run with the matching account."
        )

    tmp_dir = TMP_ROOT / account
    tmp_dir.mkdir(parents=True, exist_ok=True)
    token_path = tmp_dir / "token.json"
    gauth.save_credentials(creds, token_path)

    history_id = str(profile["historyId"])
    cursor_path = tmp_dir / "history-id"
    cursor_path.write_text(json.dumps({"historyId": history_id}))

    account_dir.mkdir(parents=True, exist_ok=True)
    (account_dir / "threads").mkdir(exist_ok=True)
    (account_dir / "drafts").mkdir(exist_ok=True)

    meta = {
        "account": account,
        "provider": "gmail",
        "poll_interval_seconds": args.poll,
        "created": date.today().isoformat(),
    }
    with meta_path.open("w") as f:
        yaml.safe_dump(meta, f, sort_keys=False)

    print(f"addemail: wrote {meta_path}")
    print(f"addemail: wrote {token_path}")
    print(f"addemail: bootstrap historyId={history_id} (cursor: {cursor_path})")
    print("addemail: restart the kernel to start ingesting mail.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
