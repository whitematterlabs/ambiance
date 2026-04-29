#!/usr/bin/env python
"""addcontact — create a person + thread in one shot.

Use when you (PAI) want to message someone who isn't in macOS Contacts and
doesn't already have a `memory/people/{slug}/` entry. Without this the raw
`mkdir messages/{slug} && echo ... >> day.md` workflow silently drops the
message: the outbound driver can't materialize meta.yaml from a slug it
can't map to a handle.

This creates:
  - memory/people/{slug}/about.yaml             (name + normalized handles)
  - communication/messages/{slug}/meta.yaml     (channel: imessage, handles)
  - communication/messages/{slug}/{slug}        (symlink to the person dir)
  - communication/messages/{slug}/YYYY-MM-DD.md (today's empty day-file)

After this, `echo "text" >> messages/{slug}/$(date +%F).md` sends through
the outbound tailer.

Usage:
    addcontact NAME HANDLE [HANDLE ...] [--slug SLUG]

Examples:
    addcontact Keezy +19492997354
    addcontact "Engin K" +19492997354 engin@example.com
    addcontact "Engin K" +19492997354 --slug keezy

Refuses to run if person or thread already exists at that slug — use
resolve-contact to rename a phone-digit slug, or edit about.yaml directly
to add another handle to an existing person.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime

from boot.messages import (
    MESSAGES_DIR,
    PEOPLE_DIR,
    _create_person,
    _create_thread,
    _normalize_handle,
    resolve_slug,
    slugify,
)


def _die(msg: str) -> None:
    print(f"addcontact: {msg}", file=sys.stderr)
    sys.exit(1)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Create a person + iMessage thread in one shot."
    )
    ap.add_argument("name", help="Display name, e.g. 'Engin K' or 'Keezy'")
    ap.add_argument("handles", nargs="+", help="Phone (+1...) or email handles")
    ap.add_argument("--slug", help="Override slug (default: slugify(name))")
    args = ap.parse_args()

    slug = args.slug.strip() if args.slug else slugify(args.name, max_words=2)
    if not slug or slug == "unknown":
        _die(f"could not derive a valid slug from {args.name!r}")

    person_dir = PEOPLE_DIR / slug
    thread_dir = MESSAGES_DIR / slug
    if person_dir.exists():
        _die(f"person already exists: {person_dir}")
    if thread_dir.exists():
        _die(f"thread already exists: {thread_dir}")

    # Reject handles already bound to a different slug — silently creating a
    # duplicate person would split the conversation across two threads.
    for h in args.handles:
        existing = resolve_slug(h)
        if existing and existing != slug:
            _die(f"handle {h!r} already routes to thread {existing!r}")

    primary, *extra = args.handles
    _create_thread(slug, primary, args.name, chat_guid=None, source="imessage")
    _create_person(slug, primary, args.name)

    # _create_* only takes one handle; backfill the rest into both files.
    if extra:
        import yaml
        norm_extra = [_normalize_handle(h) for h in extra]

        about_path = person_dir / "about.yaml"
        with about_path.open() as f:
            about = yaml.safe_load(f) or {}
        about["handles"] = (about.get("handles") or []) + norm_extra
        with about_path.open("w") as f:
            yaml.safe_dump(about, f, sort_keys=False)

        meta_path = thread_dir / "meta.yaml"
        with meta_path.open() as f:
            meta = yaml.safe_load(f) or {}
        meta["handles"] = (meta.get("handles") or []) + norm_extra
        with meta_path.open("w") as f:
            yaml.safe_dump(meta, f, sort_keys=False)

    day_file = thread_dir / f"{datetime.now().date().isoformat()}.md"
    day_file.touch()

    print(f"created person  {person_dir}")
    print(f"created thread  {thread_dir}")
    print(f"created dayfile {day_file}")
    print(f"slug: {slug}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
