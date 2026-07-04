"""menuconfig-style curses checklist with sections."""

from __future__ import annotations

import curses
from dataclasses import dataclass

from .inventory import Item


# Only drivers are surfaced as owner choices now. PAI bundles are configured by
# paiadd, not picked here; subagents are all force-installed (see below).
SECTION_TITLES = {
    "driver": "Drivers",
}

VISIBLE_KINDS = tuple(SECTION_TITLES)

# Force-installed on every setup and never shown as a choice. These are still
# *discovered* by inventory (so we can install them) — just not rendered in the
# picker or the PAI.app catalog. browse + computer-use are baseline agent
# capability; ax/calendar/imessage/notification are infrastructure drivers the
# owner shouldn't have to reason about.
AUTO_INSTALL_ITEMS = frozenset({
    ("driver", "ax"),
    ("driver", "calendar"),
    ("driver", "imessage"),
    ("driver", "notification"),
    # Voice ships as a first-class capability: both the local (whisper/`say`)
    # and cloud (OpenAI/ElevenLabs) providers install silently so the web
    # surface's Siri/ElevenLabs toggle always has both engines available.
    ("driver", "voice"),
    ("driver", "voice_cloud"),
    ("subagent", "browse"),
    ("subagent", "computer-use"),
})

# Visible drivers stay checked by default (opt-out) — the owner can uncheck the
# ones they don't want (e.g. whatsapp).
AUTO_CHECKED_KINDS = frozenset({"driver"})

# Back-compat for callers that only understand kind-level defaults.
AUTO_CHECKED = AUTO_CHECKED_KINDS


def is_hidden(kind: str, name: str) -> bool:
    """True for items force-installed silently — excluded from every picker."""
    return (kind, name) in AUTO_INSTALL_ITEMS


@dataclass
class Row:
    kind: str            # group key, used to match SECTION_TITLES
    is_header: bool
    item: Item | None    # None for header rows
    checked: bool        # ignored on header rows


def is_auto_checked(kind: str, item: Item) -> bool:
    return kind in AUTO_CHECKED_KINDS


def auto_checked_refs() -> list[str]:
    """Typed registry refs for the force-installed set, for the PAI.app twin."""
    plural = {
        "driver": "drivers",
        "pai": "pais",
        "subagent": "subagents",
    }
    return sorted(
        f"{plural.get(kind, kind + 's')}/{name}"
        for kind, name in AUTO_INSTALL_ITEMS
    )


def _build_rows(groups: dict[str, list[Item]]) -> list[Row]:
    rows: list[Row] = []
    for kind in VISIBLE_KINDS:
        items = [it for it in (groups.get(kind) or []) if not is_hidden(kind, it.name)]
        rows.append(Row(kind=kind, is_header=True, item=None, checked=False))
        for it in items:
            checked = it.installed or is_auto_checked(kind, it)
            rows.append(Row(kind=kind, is_header=False, item=it, checked=checked))
        if not items:
            # Placeholder to make absence obvious. Header alone.
            pass
    return rows


def _first_selectable(rows: list[Row]) -> int:
    for i, r in enumerate(rows):
        if not r.is_header and r.item is not None and not r.item.installed:
            return i
    # Fall back to first non-header row at all (could be all installed).
    for i, r in enumerate(rows):
        if not r.is_header:
            return i
    return 0


def _draw(stdscr, rows: list[Row], cursor: int, top: int) -> None:
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    title = "PAI Setup"
    help_line = "↑/↓ move   space toggle   enter confirm   q cancel"
    stdscr.addnstr(0, 0, title, w - 1, curses.A_BOLD)
    stdscr.addnstr(1, 0, help_line, w - 1, curses.A_DIM)

    body_top = 3
    body_h = max(1, h - body_top - 1)
    visible = rows[top : top + body_h]
    for i, row in enumerate(visible):
        y = body_top + i
        idx = top + i
        if row.is_header:
            label = SECTION_TITLES.get(row.kind, row.kind)
            stdscr.addnstr(y, 0, label, w - 1, curses.A_BOLD | curses.A_UNDERLINE)
            continue
        it = row.item
        assert it is not None
        mark = "[x]" if row.checked else "[ ]"
        suffix = "  (already installed)" if it.installed else ""
        desc = f" — {it.description}" if it.description else ""
        line = f"  {mark} {it.name}{desc}{suffix}"
        attr = curses.A_REVERSE if idx == cursor else curses.A_NORMAL
        if it.installed:
            attr |= curses.A_DIM
        stdscr.addnstr(y, 0, line, w - 1, attr)
    stdscr.refresh()


def _move(rows: list[Row], cursor: int, delta: int) -> int:
    n = len(rows)
    i = cursor
    step = 1 if delta > 0 else -1
    remaining = abs(delta)
    while remaining > 0:
        i += step
        if i < 0 or i >= n:
            return cursor
        if not rows[i].is_header:
            remaining -= 1
    return i


def run(groups: dict[str, list[Item]]) -> dict[str, list[str]] | None:
    """Run the picker. Returns {kind: [names]} of selected items, or None on cancel."""
    rows = _build_rows(groups)
    if not any(not r.is_header for r in rows):
        # Empty registry — nothing to pick.
        return {k: [] for k in VISIBLE_KINDS}

    def _curses_main(stdscr) -> dict[str, list[str]] | None:
        curses.curs_set(0)
        stdscr.keypad(True)
        cursor = _first_selectable(rows)
        top = 0
        while True:
            h, _ = stdscr.getmaxyx()
            body_h = max(1, h - 4)
            if cursor < top:
                top = cursor
            elif cursor >= top + body_h:
                top = cursor - body_h + 1
            _draw(stdscr, rows, cursor, top)
            ch = stdscr.getch()
            if ch in (curses.KEY_UP, ord("k")):
                cursor = _move(rows, cursor, -1)
            elif ch in (curses.KEY_DOWN, ord("j")):
                cursor = _move(rows, cursor, +1)
            elif ch in (curses.KEY_PPAGE,):
                cursor = _move(rows, cursor, -10)
            elif ch in (curses.KEY_NPAGE,):
                cursor = _move(rows, cursor, +10)
            elif ch == curses.KEY_HOME:
                cursor = _first_selectable(rows)
            elif ch == ord(" "):
                row = rows[cursor]
                if not row.is_header and row.item is not None and not row.item.installed:
                    row.checked = not row.checked
            elif ch in (curses.KEY_ENTER, 10, 13):
                out: dict[str, list[str]] = {k: [] for k in VISIBLE_KINDS}
                for r in rows:
                    if r.is_header or r.item is None:
                        continue
                    if r.checked and not r.item.installed:
                        out[r.kind].append(r.item.name)
                return out
            elif ch in (ord("q"), 27):  # q or ESC
                return None

    return curses.wrapper(_curses_main)
