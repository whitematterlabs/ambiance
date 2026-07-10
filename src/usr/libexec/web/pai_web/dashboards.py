"""Dashboard discovery for the PAI web surface — mirrors `schedule.py`'s role
as the single parse point for a PAI-authored on-disk artifact.

A dashboard is one self-contained file at `/var/lib/dashboards/<slug>.html`:
freeform HTML (markup + inlined CSS/JS) with an embedded manifest so it stays a
single greppable file with no sidecar:

    <script type="application/pai-dashboard+json">
    { "title": "Sales Pulse", "order": 10, "channels": ["procs", "drivers"] }
    </script>
    <!doctype html> … PAI's markup … <script> message listener … </script>

`list_dashboards()` walks the dir, parses each manifest, and projects typed rows
the console renders as tabs (one per dashboard, sorted by `order` then title).
`read_dashboard(slug)` returns the raw HTML the server frames in a sandboxed
iframe. A file with a missing/broken manifest still shows up (title falls back to
the slug, no channels) rather than vanishing — PAI gets a visible, debuggable tab
instead of a silent no-op.

The console never executes this HTML in its own origin: the server serves it
under a strict CSP + `sandbox` directive and the frontend frames it with
`sandbox="allow-scripts"` (opaque origin). See `server._dashboard` for the wire
headers.
"""

from __future__ import annotations

import json
import re
from typing import Optional

from boot import paths


# Slugs address a single file under the dashboards dir and ride straight into a
# URL path, so keep them to an unambiguous, traversal-proof alphabet.
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")

# The embedded manifest: a <script type="application/pai-dashboard+json"> block.
# Case-insensitive on the tag/attr, tolerant of attribute ordering and extra
# whitespace, non-greedy body so only the first block is taken.
_MANIFEST_RE = re.compile(
    r"<script\b[^>]*\btype\s*=\s*['\"]application/pai-dashboard\+json['\"][^>]*>"
    r"(.*?)</script\s*>",
    re.IGNORECASE | re.DOTALL,
)


def valid_slug(slug: str) -> bool:
    """Whether `slug` is a safe dashboard identifier (no path traversal)."""
    return bool(_SLUG_RE.match(slug)) and "/" not in slug and ".." not in slug


def parse_manifest(html: str) -> dict:
    """Pull the embedded JSON manifest out of a dashboard's HTML.

    Returns the decoded object, or `{}` when there is no manifest block or it
    isn't valid JSON — the caller supplies defaults so a malformed dashboard
    degrades to a slug-titled, channel-less tab rather than disappearing.
    """
    m = _MANIFEST_RE.search(html)
    if not m:
        return {}
    try:
        obj = json.loads(m.group(1).strip())
    except (json.JSONDecodeError, ValueError):
        return {}
    return obj if isinstance(obj, dict) else {}


def _row(slug: str, html: str) -> dict:
    """Project one dashboard file into a typed row for the console."""
    manifest = parse_manifest(html)

    title = manifest.get("title")
    if not isinstance(title, str) or not title.strip():
        title = slug
    title = title.strip()

    order = manifest.get("order")
    if not isinstance(order, (int, float)) or isinstance(order, bool):
        order = 100  # unordered dashboards sort after explicitly-ordered ones
    order = int(order)

    raw_channels = manifest.get("channels")
    channels: list[str] = []
    if isinstance(raw_channels, list):
        channels = [c for c in raw_channels if isinstance(c, str) and c]

    return {"slug": slug, "title": title, "order": order, "channels": channels}


def list_dashboards() -> list[dict]:
    """Every dashboard as a typed row, sorted by `order` then title.

    Walks `/var/lib/dashboards/*.html`; unreadable files and files with an
    invalid slug are skipped. A missing dir is simply an empty list.
    """
    root = paths.var_lib_dashboards()
    rows: list[dict] = []
    try:
        entries = sorted(root.glob("*.html"))
    except OSError:
        return []
    for path in entries:
        slug = path.stem
        if not valid_slug(slug):
            continue
        try:
            html = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rows.append(_row(slug, html))
    rows.sort(key=lambda r: (r["order"], r["title"].lower()))
    return rows


def read_dashboard(slug: str) -> Optional[str]:
    """Raw HTML for one dashboard, or None if the slug is invalid/missing."""
    if not valid_slug(slug):
        return None
    path = paths.var_lib_dashboards() / f"{slug}.html"
    if not path.is_file():
        return None
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
