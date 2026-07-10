#!/usr/bin/env python
"""dashboard — create, list, and remove PAI-authored console dashboards.

A dashboard is one self-contained file at `/var/lib/dashboards/<slug>.html`:
freeform HTML (markup + inlined CSS/JS) with an embedded manifest the console
reads to build a tab and bridge live data in. The web console renders each in a
hard-sandboxed iframe (opaque origin, no network) and pushes the manifest's
declared `channels` into it over postMessage — so a dashboard can only *display*
data the console already holds; its arbitrary JS is walled off from the owner
surface.

You *could* write the file directly; this bin exists so the manifest is always
valid: `write` builds the `<script type="application/pai-dashboard+json">` block
from validated flags and prepends it to the HTML body you pass on stdin (any
manifest already in the body is dropped, so there's exactly one). See the
`make-dashboards` skill for the HTML contract, the `pai:data` message protocol,
and the channels available in v1.

Usage:
    dashboard write <slug> --title T [--channel C]... [--order N] < body.html
    dashboard list
    dashboard remove <slug>

Example:
    dashboard write pulse --title "Fleet Pulse" --channel procs <<'HTML'
    <!doctype html><body><pre id="out">waiting…</pre>
    <script>
      addEventListener("message", (e) => {
        if (e.data?.type !== "pai:data" || e.data.channel !== "procs") return;
        document.getElementById("out").textContent =
          JSON.stringify(e.data.payload, null, 2);
      });
    </script></body>
    HTML
"""

from __future__ import annotations

import argparse
import json
import re
import sys

from boot import paths

# Same alphabet the console's discovery enforces (pai_web/dashboards.valid_slug):
# a single file under the dashboards dir, addressed straight in a URL path.
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")

# Strip any manifest the body already carries so the generated one is the only
# `application/pai-dashboard+json` block (the console takes the first).
_MANIFEST_RE = re.compile(
    r"<script\b[^>]*\btype\s*=\s*['\"]application/pai-dashboard\+json['\"][^>]*>"
    r".*?</script\s*>",
    re.IGNORECASE | re.DOTALL,
)


def _dash_dir():
    # Computed from PAI_ROOT directly (not a boot.paths helper) so the bin works
    # against any installed kernel build.
    return paths.PAI_ROOT / "var" / "lib" / "dashboards"


def _valid_slug(slug: str) -> bool:
    return bool(_SLUG_RE.match(slug)) and "/" not in slug and ".." not in slug


def _title_of(html: str, fallback: str) -> str:
    m = _MANIFEST_RE.search(html)
    if m:
        try:
            obj = json.loads(m.group(0).split(">", 1)[1].rsplit("</", 1)[0].strip())
            if isinstance(obj, dict) and isinstance(obj.get("title"), str):
                t = obj["title"].strip()
                if t:
                    return t
        except (json.JSONDecodeError, ValueError, IndexError):
            pass
    return fallback


def _cmd_write(args: argparse.Namespace) -> int:
    slug = args.slug
    if not _valid_slug(slug):
        print(
            f"dashboard: invalid slug {slug!r} — use lowercase letters, digits, "
            "'.', '_', '-' (must start alphanumeric)",
            file=sys.stderr,
        )
        return 1
    title = args.title.strip()
    if not title:
        print("dashboard: --title must not be empty", file=sys.stderr)
        return 1

    body = sys.stdin.read()
    if not body.strip():
        print("dashboard: empty body on stdin — pipe the dashboard HTML in", file=sys.stderr)
        return 1
    body = _MANIFEST_RE.sub("", body).lstrip("\n")

    manifest = {"title": title, "order": args.order, "channels": list(args.channel or [])}
    block = (
        '<script type="application/pai-dashboard+json">\n'
        + json.dumps(manifest, indent=2)
        + "\n</script>\n"
    )

    dash_dir = _dash_dir()
    dash_dir.mkdir(parents=True, exist_ok=True)
    path = dash_dir / f"{slug}.html"
    existed = path.exists()
    path.write_text(block + body, encoding="utf-8")
    print(
        f"dashboard: {'updated' if existed else 'created'} {slug} "
        f"(title={title!r}, channels={manifest['channels']}) → {path}"
    )
    return 0


def _cmd_list(_args: argparse.Namespace) -> int:
    dash_dir = _dash_dir()
    if not dash_dir.is_dir():
        print("dashboard: no dashboards yet")
        return 0
    rows = []
    for p in sorted(dash_dir.glob("*.html")):
        if not _valid_slug(p.stem):
            continue
        try:
            html = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rows.append((p.stem, _title_of(html, p.stem)))
    if not rows:
        print("dashboard: no dashboards yet")
        return 0
    width = max(len(slug) for slug, _ in rows)
    for slug, title in rows:
        print(f"{slug.ljust(width)}  {title}")
    return 0


def _cmd_remove(args: argparse.Namespace) -> int:
    slug = args.slug
    if not _valid_slug(slug):
        print(f"dashboard: invalid slug {slug!r}", file=sys.stderr)
        return 1
    path = _dash_dir() / f"{slug}.html"
    if not path.is_file():
        print(f"dashboard: no dashboard named {slug!r}", file=sys.stderr)
        return 1
    path.unlink()
    print(f"dashboard: removed {slug}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="dashboard", description="Create, list, and remove console dashboards."
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    w = sub.add_parser("write", help="create/update a dashboard from stdin HTML")
    w.add_argument("slug", help="dashboard id (lowercase; the tab's stable key)")
    w.add_argument("--title", required=True, help="tab label")
    w.add_argument(
        "--channel",
        action="append",
        default=[],
        metavar="NAME",
        help="a live-data channel to bridge in (repeatable)",
    )
    w.add_argument("--order", type=int, default=100, help="tab sort order (lower = earlier)")
    w.set_defaults(func=_cmd_write)

    ls = sub.add_parser("list", help="list dashboards")
    ls.set_defaults(func=_cmd_list)

    rm = sub.add_parser("remove", help="delete a dashboard")
    rm.add_argument("slug", help="dashboard id to remove")
    rm.set_defaults(func=_cmd_remove)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
