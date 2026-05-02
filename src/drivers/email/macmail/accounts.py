"""macOS Mail.app account discovery — shared by macmail-in, macmail-out, and mailsearch.

Mail.app's Envelope Index sqlite stores mailboxes by their *localized*
display name (`Gelen Kutusu`, `Gönderilmiş Öğeler`, …) and tags inbound
mail with whatever `To:` header arrived (which on iCloud is often a
Hide-My-Email relay alias rather than the canonical account address).

To get the truth we ask Mail.app itself via AppleScript and persist the
result. Both drivers and the mailsearch tool read this file instead of
guessing from header sniffing or English mailbox-name suffixes.

Schema (`{HOME}/tmp/drivers/macmail/accounts.yaml`):

    accounts:
      0A836680-...:
        addresses: [arda.tasci@icloud.com, alias@privaterelay.appleid.com]
        inbox_name: INBOX
        sent_name: Sent Messages
      BFFD063A-...:
        addresses: [ardatasci@outlook.com]
        inbox_name: Gelen Kutusu
        sent_name: Gönderilmiş Öğeler
"""

from __future__ import annotations

import asyncio
import os
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import quote

import yaml

from boot import processes as P


ACCOUNTS_PATH = P.HOME_DIR / "tmp" / "drivers" / "macmail" / "accounts.yaml"


# Mail.app reports mailbox names in NFC; the Envelope Index URL stores
# them URL-encoded from NFD bytes (Apple convention). Normalize before
# encoding so our LIKE patterns match the on-disk URLs.
def _url_encode_mailbox_name(name: str) -> str:
    return quote(unicodedata.normalize("NFD", name), safe="")


@dataclass
class Account:
    uuid: str
    addresses: list[str] = field(default_factory=list)  # primary first
    inbox_name: Optional[str] = None
    sent_name: Optional[str] = None


@dataclass
class AccountsConfig:
    accounts: dict[str, Account] = field(default_factory=dict)

    # ---- public lookups (used by the drivers and mailsearch) -------------

    def address_for_uuid(self, uuid: str) -> Optional[str]:
        a = self.accounts.get(uuid)
        if a is None or not a.addresses:
            return None
        return a.addresses[0]

    def accepts_from(self, address: str) -> bool:
        """True if `address` is any address Mail.app reports for any account.

        Includes aliases, so iCloud Hide-My-Email relay addresses that the
        user can legitimately send from are accepted.
        """
        if not address:
            return False
        addr = address.strip().lower()
        for a in self.accounts.values():
            for x in a.addresses:
                if x.lower() == addr:
                    return True
        return False

    def all_addresses(self) -> list[str]:
        return sorted({x.lower() for a in self.accounts.values() for x in a.addresses})

    def url_like_patterns(self) -> list[tuple[str, str]]:
        """Yield (pattern, role) tuples for the SQL filter.

        Each pattern is `%{uuid}%/{url-encoded-mailbox-name}` — matches the
        Envelope Index `mailboxes.url` column for that account's inbox or
        sent mailbox in any locale and any URL scheme (imap, ews, …).
        Role is "inbound" or "outbound".
        """
        out: list[tuple[str, str]] = []
        for acc in self.accounts.values():
            if acc.inbox_name:
                out.append((f"%{acc.uuid}%/{_url_encode_mailbox_name(acc.inbox_name)}", "inbound"))
            if acc.sent_name:
                out.append((f"%{acc.uuid}%/{_url_encode_mailbox_name(acc.sent_name)}", "outbound"))
        return out

    def role_for_url(self, url: str) -> Optional[str]:
        """Classify a `mailboxes.url` value as "inbound" / "outbound" / None."""
        for pat, role in self.url_like_patterns():
            # Translate SQL LIKE semantics to Python: `%X%` → substring match.
            # Our patterns are `%uuid%/name` with exactly two `%` segments at
            # the start and middle (no `%` inside name after URL-encoding).
            # So: split on `%`, every fragment must appear in order in url.
            ok = True
            cursor = 0
            for fragment in pat.split("%"):
                if not fragment:
                    continue
                idx = url.find(fragment, cursor)
                if idx < 0:
                    ok = False
                    break
                cursor = idx + len(fragment)
            if ok:
                return role
        return None

    def is_empty(self) -> bool:
        return not self.accounts


# ---------- AppleScript -----------------------------------------------------

# Single AppleScript call returns three sections joined by newlines:
#   ADDR|<uuid>|<address>     (one line per address per account)
#   INBOX|<uuid>|<name>       (one line per per-account inbox)
#   SENT|<uuid>|<name>        (one line per per-account sent mailbox)
_DISCOVERY_SCRIPT = (
    'tell application "Mail"\n'
    '  set out to ""\n'
    '  repeat with a in accounts\n'
    '    set u to id of a\n'
    '    set addrList to email addresses of a\n'
    '    if addrList is not missing value then\n'
    '      repeat with addr in addrList\n'
    '        set out to out & "ADDR|" & u & "|" & (contents of addr) & linefeed\n'
    '      end repeat\n'
    '    end if\n'
    '  end repeat\n'
    '  repeat with mb in (every mailbox of inbox)\n'
    '    try\n'
    '      set u to id of (account of mb)\n'
    '      set out to out & "INBOX|" & u & "|" & (name of mb) & linefeed\n'
    '    end try\n'
    '  end repeat\n'
    '  repeat with mb in (every mailbox of sent mailbox)\n'
    '    try\n'
    '      set u to id of (account of mb)\n'
    '      set out to out & "SENT|" & u & "|" & (name of mb) & linefeed\n'
    '    end try\n'
    '  end repeat\n'
    '  return out\n'
    'end tell'
)


async def _run_osascript(script: str) -> tuple[int, str, str]:
    """Run an AppleScript via osascript -e. Returns (returncode, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        "osascript", "-e", script,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return (
        proc.returncode if proc.returncode is not None else -1,
        stdout.decode("utf-8", errors="replace"),
        stderr.decode("utf-8", errors="replace").strip(),
    )


def parse_discovery_output(text: str) -> AccountsConfig:
    """Parse the `ADDR|/INBOX|/SENT|` line stream into an AccountsConfig.

    Public so tests can drive it with canned osascript output.
    """
    accounts: dict[str, Account] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        parts = line.split("|", 2)
        if len(parts) != 3:
            continue
        kind, uuid, value = parts[0], parts[1].strip(), parts[2].strip()
        if not uuid or not value:
            continue
        acc = accounts.setdefault(uuid, Account(uuid=uuid))
        if kind == "ADDR":
            if value not in acc.addresses:
                acc.addresses.append(value)
        elif kind == "INBOX":
            acc.inbox_name = value
        elif kind == "SENT":
            acc.sent_name = value
    return AccountsConfig(accounts=accounts)


# ---------- persistence ----------------------------------------------------

def _atomic_dump(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w") as f:
        yaml.safe_dump(data, f, sort_keys=True, allow_unicode=True)
    os.replace(tmp, path)


def _to_yaml(cfg: AccountsConfig) -> dict:
    return {
        "accounts": {
            uuid: {
                "addresses": list(acc.addresses),
                "inbox_name": acc.inbox_name,
                "sent_name": acc.sent_name,
            }
            for uuid, acc in sorted(cfg.accounts.items())
        }
    }


def _from_yaml(data: dict) -> AccountsConfig:
    raw = data.get("accounts") if isinstance(data, dict) else None
    if not isinstance(raw, dict):
        # Either empty or the old flat `{uuid: address}` schema. Discard;
        # refresh() will repopulate from Mail.app.
        return AccountsConfig()
    accounts: dict[str, Account] = {}
    for uuid, body in raw.items():
        if not isinstance(body, dict):
            continue
        addrs = body.get("addresses") or []
        if not isinstance(addrs, list):
            addrs = []
        accounts[uuid] = Account(
            uuid=uuid,
            addresses=[str(a) for a in addrs],
            inbox_name=body.get("inbox_name") or None,
            sent_name=body.get("sent_name") or None,
        )
    return AccountsConfig(accounts=accounts)


def load() -> AccountsConfig:
    """Read the persisted config. Empty config if missing or unreadable."""
    if not ACCOUNTS_PATH.exists():
        return AccountsConfig()
    try:
        with ACCOUNTS_PATH.open() as f:
            data = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        return AccountsConfig()
    return _from_yaml(data)


async def refresh() -> AccountsConfig:
    """Ask Mail.app via AppleScript, persist, return.

    On any failure (Mail.app not running, no automation permission, parse
    error) returns the previously-persisted config so callers stay
    operational with whatever they had.
    """
    code, stdout, stderr = await _run_osascript(_DISCOVERY_SCRIPT)
    if code != 0:
        print(f"[macmail-accounts] discovery failed: {stderr}", flush=True)
        return load()
    cfg = parse_discovery_output(stdout)
    if cfg.is_empty():
        print(f"[macmail-accounts] discovery returned no accounts (stderr={stderr!r})", flush=True)
        return load()
    try:
        _atomic_dump(ACCOUNTS_PATH, _to_yaml(cfg))
    except OSError as e:
        print(f"[macmail-accounts] could not write {ACCOUNTS_PATH}: {e}", flush=True)
    return cfg


def summarize(cfg: AccountsConfig) -> str:
    """Short human-readable line for boot logging."""
    parts = []
    for uuid, acc in sorted(cfg.accounts.items()):
        primary = acc.addresses[0] if acc.addresses else "?"
        parts.append(f"{primary} (inbox={acc.inbox_name!r}, sent={acc.sent_name!r})")
    return ", ".join(parts) if parts else "<none>"


__all__ = [
    "Account",
    "AccountsConfig",
    "ACCOUNTS_PATH",
    "load",
    "refresh",
    "parse_discovery_output",
    "summarize",
]
