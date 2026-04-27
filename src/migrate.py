"""
Migrate 2026 data from animus twin.json into PAI's home/ filesystem.

Explodes the graph into:
  - communication/messages/{thread}/YYYY-MM-DD.md  (message logs)
  - communication/messages/{thread}/meta.yaml       (thread metadata)
  - communication/messages/{thread}/{member} ->     (symlinks to people)
  - memory/people/{name}/about.yaml                 (contact profiles)
  - memory/topics/{slug}/meta.yaml                  (topic metadata)
  - memory/topics/{slug}/{date}/summary.md          (topic summaries)
  - memory/topics/{slug}/{date}/{thread}.md ->      (symlinks to messages)
  - memory/myself/identity.yaml                     (owner identity)
  - memory/myself/directives.md                     (behavioral directives)
"""

import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ANIMUS_GRAPH = Path.home() / "Projects" / "animus" / "memory" / "twin.json"
HOME_DIR = Path(__file__).resolve().parent.parent / "home"
YEAR = "2026"


FILLER_WORDS = {
    "a", "an", "the", "and", "or", "but", "for", "from", "about",
    "with", "into", "over", "after", "before", "during", "between",
    "of", "on", "in", "to", "at", "by", "as", "is", "are", "was",
    "their", "his", "her", "its",
}


def slugify(name: str, *, max_words: int = 0) -> str:
    """alice smith -> alice-smith, 4KM OBeezy -> 4km-obeezy

    max_words: if > 0, drop filler words and truncate to N tokens.
    """
    s = name.strip().lower()
    s = re.sub(r"[\u2019']s\b", "s", s)  # boyfriend's -> boyfriends
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    if max_words > 0:
        tokens = [t for t in s.split("-") if t not in FILLER_WORDS]
        s = "-".join(tokens[:max_words])
    return s


def load_graph():
    print(f"Loading {ANIMUS_GRAPH} ...")
    with open(ANIMUS_GRAPH) as f:
        data = json.load(f)
    nodes = {n["id"]: n for n in data["nodes"]}
    edges = data["edges"]
    date_index = data.get("_date_index", {})
    print(f"  {len(nodes)} nodes, {len(edges)} edges, {len(date_index)} dates")
    return nodes, edges, date_index


def resolve_display_name(nodes: dict, node_id: str) -> str:
    """Get display name for a contact, falling back to the identifier."""
    node = nodes.get(node_id, {})
    name = node.get("display_name")
    if name:
        return name
    handles = node.get("handles", [])
    if handles:
        return handles[0].get("identifier", node_id)
    return node_id


def resolve_sender(nodes: dict, sender: str) -> str:
    """Resolve a message sender to a lowercase first name or 'me'."""
    if sender == "me":
        return "me"
    name = resolve_display_name(nodes, sender)
    # Use first name, lowercased
    return name.split()[0].lower() if name else sender


def get_thread_slug(nodes: dict, thread_id: str) -> str:
    """Generate a filesystem-safe slug for a thread."""
    name = resolve_display_name(nodes, thread_id)
    return slugify(name)


def get_group_members(edges: list, group_id: str) -> list[str]:
    """Return contact node IDs that are members of a group."""
    return [
        e["source"]
        for e in edges
        if e["target"] == group_id
        and e.get("rel") == "member_of"
        and e["source"] != "me"
    ]


def format_timestamp(iso_ts: str) -> str:
    """Extract HH:MM from an ISO timestamp."""
    try:
        dt = datetime.fromisoformat(iso_ts)
        return dt.strftime("%H:%M")
    except (ValueError, TypeError):
        return "00:00"


def walk_messages_2026(nodes: dict, thread_id: str):
    """Walk the linked list for a thread, yielding 2026 messages."""
    thread = nodes.get(thread_id, {})
    current_id = thread.get("head_msg")
    visited = set()

    while current_id:
        if current_id in visited:
            break
        visited.add(current_id)
        node = nodes.get(current_id)
        if not node:
            break

        date_key = node.get("date_key", "")
        if date_key.startswith(YEAR):
            yield node, current_id
        elif date_key > YEAR:
            break

        current_id = node.get("next_msg")


def write_messages(nodes: dict, edges: list):
    """Write message day-files, meta.yaml, and people symlinks per thread."""
    threads = [
        n for n in nodes.values()
        if n.get("type") in ("contact", "group")
        and (n.get("last_seen") or "") >= YEAR
    ]

    print(f"Processing {len(threads)} threads with 2026 activity ...")
    thread_slugs = {}  # thread_id -> slug (for topic symlinks later)
    date_threads: dict[str, set[str]] = defaultdict(set)  # date -> set of thread slugs
    msg_count = 0
    file_count = 0

    for thread in threads:
        thread_id = thread["id"]
        is_group = thread["type"] == "group"

        # Bucket messages by date
        by_date: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
        for msg, msg_id in walk_messages_2026(nodes, thread_id):
            content = msg.get("content")
            if not content:
                continue
            ts = format_timestamp(msg.get("timestamp", ""))
            sender = resolve_sender(nodes, msg.get("sender", "me"))
            by_date[msg["date_key"]].append((ts, sender, content))
            msg_count += 1

        if not by_date:
            continue

        slug = get_thread_slug(nodes, thread_id)
        # Handle duplicate slugs
        base_slug = slug
        counter = 2
        while slug in thread_slugs.values():
            slug = f"{base_slug}-{counter}"
            counter += 1
        thread_slugs[thread_id] = slug

        thread_dir = HOME_DIR / "communication" / "messages" / slug
        thread_dir.mkdir(parents=True, exist_ok=True)

        # Write day files
        for date_key in sorted(by_date):
            lines = [f"[{ts}] {sender}: {content}" for ts, sender, content in by_date[date_key]]
            day_file = thread_dir / f"{date_key}.md"
            day_file.write_text("\n".join(lines) + "\n")
            file_count += 1
            date_threads[date_key].add(slug)

        # Write meta.yaml
        meta_path = thread_dir / "meta.yaml"
        display = resolve_display_name(nodes, thread_id)
        desc = thread.get("summary") or f"Conversation with {display}"
        created = (thread.get("first_seen") or "")[:10]

        if is_group:
            members = get_group_members(edges, thread_id)
            member_slugs = [slugify(resolve_display_name(nodes, m)) for m in members]
            meta_lines = [
                f"description: {desc}",
                f"created: {created}",
                "group: true",
                "members:",
            ] + [f"  - {s}" for s in member_slugs]
        else:
            meta_lines = [
                f"description: {desc}",
                f"created: {created}",
                "group: false",
            ]
        meta_path.write_text("\n".join(meta_lines) + "\n")

        # Symlinks to memory/people/
        if is_group:
            members = get_group_members(edges, thread_id)
        else:
            members = [thread_id]  # the contact itself

        for member_id in members:
            member_slug = slugify(resolve_display_name(nodes, member_id))
            link_path = thread_dir / member_slug
            target = f"../../../memory/people/{member_slug}/"
            if not link_path.exists():
                try:
                    link_path.symlink_to(target)
                except OSError:
                    pass

    print(f"  {msg_count} messages -> {file_count} day-files across {len(thread_slugs)} threads")
    return thread_slugs, dict(date_threads)


def write_people(nodes: dict):
    """Write memory/people/{slug}/about.yaml for each contact."""
    contacts = [n for n in nodes.values() if n.get("type") == "contact"]
    people_dir = HOME_DIR / "memory" / "people"
    count = 0

    for contact in contacts:
        name = resolve_display_name(nodes, contact["id"])
        slug = slugify(name)
        person_dir = people_dir / slug
        person_dir.mkdir(parents=True, exist_ok=True)

        about_path = person_dir / "about.yaml"
        summary = contact.get("summary") or ""

        lines = [f"name: {name}"]

        # Add handles as context
        handles = contact.get("handles", [])
        if handles:
            h = handles[0]
            platform = h.get("platform", "")
            identifier = h.get("identifier", "")
            if platform and identifier:
                lines.append(f"handle: {identifier}")

        lines.append(f"relationship: {summary}" if summary else "relationship:")
        lines.append("entry: |")
        if summary:
            lines.append(f"  {summary}")

        about_path.write_text("\n".join(lines) + "\n")
        count += 1

    print(f"  {count} people profiles written")


def write_topics(nodes: dict, thread_slugs: dict):
    """Write memory/topics/{slug}/ directories with meta.yaml and summaries."""
    topics = [
        n for n in nodes.values()
        if n.get("type") == "topic"
        and any(
            isinstance(tr, str) and tr.startswith(YEAR)
            for tr in (n.get("time_range") or [])
        )
    ]

    print(f"Processing {len(topics)} topics from 2026 ...")
    topics_dir = HOME_DIR / "memory" / "topics"
    used_slugs = set()
    count = 0

    for topic in topics:
        title = topic.get("title", "untitled")
        slug = slugify(title, max_words=5)
        if not slug:
            continue
        # Deduplicate
        base_slug = slug[:60]
        slug = base_slug
        counter = 2
        while slug in used_slugs:
            slug = f"{base_slug}-{counter}"
            counter += 1
        used_slugs.add(slug)

        topic_dir = topics_dir / slug
        topic_dir.mkdir(parents=True, exist_ok=True)

        # meta.yaml
        time_range = topic.get("time_range") or ["", ""]
        start_date = time_range[0][:10] if time_range[0] else ""
        status = "resolved"  # historical data
        people_ids = topic.get("people", [])
        people_slugs = [
            slugify(resolve_display_name(nodes, pid))
            for pid in people_ids
            if pid in nodes
        ]

        meta_lines = [
            f"name: {title}",
            f"status: {status}",
        ]
        if people_slugs:
            meta_lines.append("people:")
            meta_lines.extend(f"  - {p}" for p in people_slugs)
        if start_date:
            meta_lines.append(f"created: {start_date}")

        (topic_dir / "meta.yaml").write_text("\n".join(meta_lines) + "\n")

        # Bucket message_ids by date -> set of thread slugs
        date_to_threads: dict[str, set[str]] = defaultdict(set)
        for mid in topic.get("message_ids", []):
            msg = nodes.get(mid)
            if not msg:
                continue
            date_key = msg.get("date_key", "")
            if not date_key.startswith(YEAR):
                continue
            # Resolve thread from sender or people list
            sender = msg.get("sender", "")
            if sender != "me" and sender in thread_slugs:
                date_to_threads[date_key].add(thread_slugs[sender])
            else:
                for pid in people_ids:
                    if pid in thread_slugs:
                        date_to_threads[date_key].add(thread_slugs[pid])

        # Create a date subdir per unique date
        summary = topic.get("summary", "")
        key_points = topic.get("key_points", [])

        for date_key in sorted(date_to_threads):
            date_dir = topic_dir / date_key
            date_dir.mkdir(exist_ok=True)

            # summary.md
            summary_lines = [summary] if summary else []
            for kp in key_points:
                summary_lines.append(f"- {kp}")
            (date_dir / "summary.md").write_text("\n".join(summary_lines) + "\n")

            # Symlinks to each thread's day-file
            for thread_slug in sorted(date_to_threads[date_key]):
                link_path = date_dir / f"{thread_slug}.md"
                target = f"../../../../communication/messages/{thread_slug}/{date_key}.md"
                if not link_path.exists():
                    try:
                        link_path.symlink_to(target)
                    except OSError:
                        pass

        count += 1

    print(f"  {count} topics written")


def write_myself():
    """Write memory/myself/ identity and directives."""
    myself_dir = HOME_DIR / "memory" / "myself"
    myself_dir.mkdir(parents=True, exist_ok=True)

    identity = myself_dir / "identity.yaml"
    if not identity.exists():
        identity.write_text(
            "name: Arda\n"
            "age: 22\n"
            "location: San Francisco\n"
            "hometown: Istanbul\n"
            "languages:\n"
            "  - English\n"
            "  - Turkish\n"
        )

    directives = myself_dir / "directives.md"
    if not directives.exists():
        directives.write_text(
            "- Always say yes to playing basketball\n"
            "- Maintain a chill, casual tone\n"
            "- Don't over-explain things\n"
            "- If someone asks to hang out, lean towards yes\n"
            "- Keep messages short — no walls of text\n"
        )

    print("  myself/ written")


def write_journal(date_threads: dict[str, set[str]]):
    """Write memory/journal/{date}/ with symlinks to each thread's day-file."""
    journal_dir = HOME_DIR / "memory" / "journal"
    count = 0

    for date_key in sorted(date_threads):
        day_dir = journal_dir / date_key
        day_dir.mkdir(parents=True, exist_ok=True)

        for thread_slug in sorted(date_threads[date_key]):
            link_path = day_dir / f"{thread_slug}.md"
            target = f"../../../communication/messages/{thread_slug}/{date_key}.md"
            if not link_path.exists():
                try:
                    link_path.symlink_to(target)
                except OSError:
                    pass

        # notes.md placeholder — agent writes reflections here
        notes_path = day_dir / "notes.md"
        if not notes_path.exists():
            notes_path.write_text("")

        count += 1

    print(f"  {count} journal days written")


def write_scaffold_dirs():
    """Ensure all top-level dirs exist."""
    for d in ["communication/messages", "memory/people", "memory/topics",
              "memory/journal", "memory/myself", "memory/skills",
              "tmp", "workspace"]:
        (HOME_DIR / d).mkdir(parents=True, exist_ok=True)


def main():
    nodes, edges, date_index = load_graph()

    print()
    write_scaffold_dirs()
    write_myself()

    print()
    thread_slugs, date_threads = write_messages(nodes, edges)

    print()
    write_people(nodes)

    print()
    write_topics(nodes, thread_slugs)

    print()
    write_journal(date_threads)

    print()
    print("Done.")


if __name__ == "__main__":
    main()
