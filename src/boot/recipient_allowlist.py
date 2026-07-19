"""Recipient matching for owner send allowlists (`send_allowlist:`).

Consulted by the send drivers in `ask` mode: a send whose recipient(s)
match the channel's rules goes straight out; anything else stages for
owner approval. Deliberately conservative, like cmd_allowlist: a
non-match costs one approval click, a false match sends unreviewed.

Rule forms:
- phone (imessage/whatsapp): any formatting, compared digit-for-digit —
  no country-code guessing ("5551234567" does not match "+15551234567").
- email handle / chat guid (imessage): case-insensitive exact string.
- WhatsApp JID: phone rules match `<digits>@s.whatsapp.net`; group JIDs
  (`@g.us`) match only as exact strings.
- email: exact address (case-insensitive) or `*@domain.com` — exact
  domain, no subdomains; EVERY recipient must match some rule.
"""

from __future__ import annotations

import re

_PHONE_CHARS = re.compile(r"^[+\d\s().-]+$")
_MIN_PHONE_DIGITS = 7

_WHATSAPP_USER_DOMAIN = "@s.whatsapp.net"


def normalize_phone(value: str) -> str | None:
    """Digits of a phone-looking string, else None. Fail-closed: anything
    with non-phone characters (letters, `@`, `;`) is not a phone."""
    s = (value or "").strip()
    if not s or not _PHONE_CHARS.match(s):
        return None
    digits = re.sub(r"\D", "", s)
    if len(digits) < _MIN_PHONE_DIGITS:
        return None
    return digits


def _clean_rules(rules: list[str] | None) -> list[str]:
    return [r.strip() for r in rules or [] if isinstance(r, str) and r.strip()]


def handle_allowed(candidate: str, rules: list[str] | None) -> bool:
    """True iff a single delivery target (imessage handle/chat guid,
    WhatsApp JID) matches some rule."""
    cand = (candidate or "").strip()
    clean = _clean_rules(rules)
    if not cand or not clean:
        return False
    cand_cmp = cand.lower()
    cand_phone = normalize_phone(cand)
    if cand_phone is None and cand_cmp.endswith(_WHATSAPP_USER_DOMAIN):
        cand_phone = normalize_phone(cand[: -len(_WHATSAPP_USER_DOMAIN)])
    for rule in clean:
        if rule.lower() == cand_cmp:
            return True
        rule_phone = normalize_phone(rule)
        if rule_phone is not None and rule_phone == cand_phone:
            return True
    return False


def _extract_address(entry: str) -> str | None:
    """Bare lowercase address from `a@b` or `Name <a@b>`; None when it
    doesn't look like exactly one address."""
    s = (entry or "").strip()
    if "<" in s or ">" in s:
        m = re.fullmatch(r"[^<>]*<([^<>]+)>", s)
        if not m:
            return None
        s = m.group(1).strip()
    if s.count("@") != 1 or s.startswith("@") or s.endswith("@") or " " in s:
        return None
    return s.lower()


def emails_allowed(addresses: list[str] | None, rules: list[str] | None) -> bool:
    """True iff there is at least one recipient and EVERY recipient
    matches some rule (exact address or `*@domain.com`)."""
    clean = _clean_rules(rules)
    entries = [a for a in addresses or [] if isinstance(a, str) and a.strip()]
    if not entries or not clean:
        return False
    exact: set[str] = set()
    domains: set[str] = set()
    for rule in clean:
        r = rule.lower()
        if r.startswith("*@"):
            dom = r[2:]
            if dom and "@" not in dom and " " not in dom:
                domains.add(dom)
        else:
            addr = _extract_address(r)
            if addr:
                exact.add(addr)
    for entry in entries:
        addr = _extract_address(entry)
        if addr is None:
            return False
        if addr in exact:
            continue
        if addr.split("@", 1)[1] in domains:
            continue
        return False
    return True
