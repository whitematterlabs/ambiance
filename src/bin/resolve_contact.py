#!/usr/bin/env python
"""resolve-contact — rename a phone-number-slug thread to a named slug.

When iMessage inbound creates a thread for a sender who isn't in macOS
Contacts, the thread directory and the person entry are named after the
raw phone digits (e.g. 17147853574). Once you learn who they are, run
this to rename both in one go.

The phone number lives in memory/people/{slug}/about.yaml under `handles:`.
This script preserves it, so the outbound driver (which reads handles
from about.yaml) keeps routing correctly after the rename.

Usage:
    resolve-contact OLD_SLUG NEW_NAME

Examples:
    resolve-contact 17147853574 Alper
    resolve-contact 17147853574 "Alper Yilmaz"

Refuses to run if the target slug already exists.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

from drivers.messages import MESSAGES_DIR, PEOPLE_DIR, slugify


def _die(msg: str) -> None:
    print(f"resolve-contact: {msg}", file=sys.stderr)
    sys.exit(1)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Rename a phone-number thread to a named slug."
    )
    ap.add_argument("old_slug", help="Current slug (phone digits, e.g. 17147853574)")
    ap.add_argument("new_name", help="Human name, e.g. 'Alper' or 'Alper Yilmaz'")
    args = ap.parse_args()

    old = args.old_slug.strip()
    new = slugify(args.new_name, max_words=2)
    if not new or new == "unknown":
        _die(f"could not derive a valid slug from {args.new_name!r}")
    if old == new:
        _die("old and new slug are the same")

    old_person = PEOPLE_DIR / old
    new_person = PEOPLE_DIR / new
    old_thread = MESSAGES_DIR / old
    new_thread = MESSAGES_DIR / new

    if not old_person.is_dir():
        _die(f"no person dir at {old_person}")
    if not old_thread.is_dir():
        _die(f"no thread dir at {old_thread}")
    if new_person.exists():
        _die(f"target person dir already exists: {new_person}")
    if new_thread.exists():
        _die(f"target thread dir already exists: {new_thread}")

    old_person.rename(new_person)
    print(f"moved {old_person} -> {new_person}")

    about_path = new_person / "about.yaml"
    if about_path.exists():
        with about_path.open() as f:
            about = yaml.safe_load(f) or {}
        about["name"] = args.new_name
        with about_path.open("w") as f:
            yaml.safe_dump(about, f, sort_keys=False)
        print(f"set name -> {args.new_name!r} in {about_path}")

    old_thread.rename(new_thread)
    print(f"moved {old_thread} -> {new_thread}")

    old_link = new_thread / old
    new_link = new_thread / new
    if old_link.is_symlink() or old_link.exists():
        old_link.unlink()
    if not new_link.exists():
        target = Path("..") / ".." / ".." / "memory" / "people" / new
        new_link.symlink_to(target)
        print(f"rewired symlink {new_link} -> {target}")

    meta = new_thread / "meta.yaml"
    if meta.exists():
        meta.unlink()
        print(f"removed stale {meta} (outbound will regenerate on next send)")

    print(f"resolved {old} -> {new}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
