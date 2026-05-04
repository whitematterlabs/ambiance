"""Custom widgets for the PAI operator console.

Thin subclasses — the app wires watchers into these via plain method calls.
"""

from __future__ import annotations

import re
from datetime import datetime

from rich.markdown import Markdown
from rich.text import Text
from textual.widgets import DataTable, RichLog

from .state import EventSighting, MeSnapshot, ProcRow


class ChatPane(RichLog):
    """Scrollable view of the me/{pid} thread, styled per speaker."""

    DEFAULT_CSS = """
    ChatPane {
        background: $surface;
        color: $text;
        padding: 0 1;
    }
    """

    def __init__(self, pid: int | None = None, **kwargs) -> None:
        super().__init__(**kwargs)
        self.pid = pid

    def render_snapshot(self, snap: MeSnapshot) -> None:
        self.clear()
        for i, msg in enumerate(snap.messages):
            header, body = self._style_message(msg)
            self.write(header)
            if body is not None:
                self.write(body)
            if i < len(snap.messages) - 1:
                self.write(Text(""))

    @staticmethod
    def _style_message(msg: str) -> tuple[Text, Markdown | None]:
        # Split "[HH:MM] sender: <body>" into a styled header and a
        # Markdown body. Body may span multiple lines; markdown rendering
        # turns headings/lists/fences into proper Rich renderables.
        first_line_end = msg.find("\n")
        head = msg if first_line_end < 0 else msg[:first_line_end]
        try:
            rb = head.index("] ")
            colon = head.index(":", rb)
            sender = head[rb + 2 : colon].strip().lower()
        except ValueError:
            return Text(msg), None
        style = "bold cyan"
        if sender == "me":
            style = "bold green"
        elif sender == "pai":
            style = "bold magenta"
        elif sender.startswith("[kernel"):
            style = "dim"
        header_str = head[: colon + 1]
        header = Text(header_str)
        header.stylize(style, rb + 2, colon + 1)
        header.stylize("dim", 0, rb + 1)  # the [HH:MM]

        # Body = everything after "sender: " on the first line, plus all
        # subsequent lines. The space after the colon (if present) is the
        # author's separator, not body content.
        after_colon = head[colon + 1 :]
        first_body = after_colon[1:] if after_colon.startswith(" ") else after_colon
        rest = "" if first_line_end < 0 else msg[first_line_end:]
        body_str = first_body + rest
        if body_str.strip() == "":
            return header, None
        return header, Markdown(body_str)


class ProcList(DataTable):
    """Running processes, latest-deadline-first."""

    DEFAULT_CSS = """
    ProcList {
        height: 1fr;
    }
    """

    def on_mount(self) -> None:
        self.cursor_type = "row"
        self.zebra_stripes = True
        self.add_columns("slug", "pid", "type", "parent", "ctx", "when")

    def render_rows(self, rows: list[ProcRow]) -> None:
        self.clear()
        # Rows arrive pre-ordered by ProcWatcher (tree pre-order: roots
        # first, subagents indented under their parent via tree_prefix).
        for r in rows:
            when = _short_when(r.when)
            slug = f"{r.tree_prefix}{r.slug}"
            self.add_row(slug, r.pid or "-", r.type, r.parent or "-", _fmt_ctx(r.ctx_tokens), when, key=r.slug)

    def zero_ctx(self, slug: str) -> None:
        """Immediately blank the ctx cell for `slug` — called after a clear/compact."""
        try:
            self.update_cell(slug, "ctx", "-")
        except Exception:
            pass


def _fmt_ctx(n: int) -> str:
    """Compact token count: '-' if zero, else '12.3k' / '187k' / '1.2M'."""
    if not n:
        return "-"
    if n < 1000:
        return str(n)
    if n < 10_000:
        return f"{n / 1000:.1f}k"
    if n < 1_000_000:
        return f"{n // 1000}k"
    return f"{n / 1_000_000:.1f}M"


class EventStrip(RichLog):
    """Recent events scrolling by."""

    DEFAULT_CSS = """
    EventStrip {
        height: 8;
        background: $surface;
        color: $text-muted;
        border-top: solid $panel;
        padding: 0 1;
    }
    """

    def write_sighting(self, sight: EventSighting) -> None:
        stamp = sight.at.strftime("%H:%M:%S")
        payload = sight.payload
        # If the kernel consumed the file before we could read it, recover
        # what we can from the filename: "{ts}-{source}.yaml".
        if payload.get("_gone"):
            suffix = sight.filename.rsplit("-", 1)[-1]
            source = suffix.removesuffix(".yaml") or "?"
            kind = "(consumed)"
        else:
            source = str(payload.get("source", "?"))
            kind = str(payload.get("kind", "?"))
        target = payload.get("thread") or payload.get("handle") or payload.get("slug") or ""
        label = kind if kind.startswith(f"{source}:") else f"{source}:{kind}"
        line = Text()
        line.append(stamp + " ", style="dim")
        line.append(label, style="yellow")
        if target:
            line.append(f" → {target}", style="white")
        self.write(line)


class LogTail(RichLog):
    """Tail of home/tmp/kernel.log, colored by speaker tag."""

    DEFAULT_CSS = """
    LogTail {
        height: 1fr;
        background: $surface;
        border-top: solid $panel;
        padding: 0 1;
    }
    """

    def write_line(self, line: str) -> None:
        t = Text(line)
        if line.startswith("[kernel]"):
            t.stylize("dim cyan", 0, 8)
        else:
            m = _PAI_PREFIX.match(line)
            if m:
                t.stylize("bold magenta", 0, m.end())
        self.write(t)


class PaiActivity(RichLog):
    """Live view of what PAI is doing — nudges + each shell command with
    its exit status. Output bodies are elided to keep the pane readable."""

    DEFAULT_CSS = """
    PaiActivity {
        height: 2fr;
        background: $surface;
        border-top: solid $panel;
        padding: 0 1;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        # Track the in-flight command so we can update its status marker
        # when [exit N] comes in. RichLog is append-only, so we just print
        # a closing line when the exit arrives.
        self._in_command = False
        self._out_lines = 0

    def ingest(self, line: str) -> None:
        if line.startswith("--- pai supervisor"):
            self.write(Text(line, style="dim"))
            self._in_command = False
            return

        if line.startswith("[kernel] nudge:"):
            self.write(Text("", style=""))
            t = Text("> ", style="bold yellow")
            t.append(line[len("[kernel] "):], style="yellow")
            self.write(t)
            self._in_command = False
            return

        if line.startswith("[kernel] nudge failed"):
            t = Text("! ", style="bold red")
            t.append(line[len("[kernel] "):], style="red")
            self.write(t)
            self._in_command = False
            return

        if line.startswith("[kernel] nudge complete"):
            self.write(Text("  done.", style="dim green"))
            self._in_command = False
            return

        m = _PAI_PREFIX.match(line)
        if m:
            pid = m.group("pid") or ""
            rest = line[m.end():].lstrip(" ")
            tag = f"pai:{pid}" if pid else "pai"
            if rest.startswith("$ "):
                cmd = rest[2:]
                t = Text(f"  [{tag}] $ ", style="bold cyan")
                t.append(cmd, style="cyan")
                self.write(t)
                self._in_command = True
                self._out_lines = 0
                return
            t = Text(f"  {tag}: ", style="bold magenta")
            t.append(rest, style="magenta")
            self.write(t)
            self._in_command = False
            return

        if self._in_command:
            stripped = line.strip()
            if stripped.startswith("[exit"):
                code_text = stripped.strip("[]")  # "exit N"
                code = code_text.split()[-1] if code_text else "?"
                ok = code == "0"
                mark = "ok" if ok else "fail"
                style = "green" if ok else "red"
                self.write(Text(f"    {mark} ({code_text})", style=style))
                self._in_command = False
                return
            if stripped == "[stderr]":
                return
            # Elide body: show up to 2 lines, then "…"
            if self._out_lines < 2:
                preview = stripped if len(stripped) <= 80 else stripped[:77] + "…"
                self.write(Text(f"    {preview}", style="dim"))
                self._out_lines += 1
            elif self._out_lines == 2:
                self.write(Text("    …", style="dim"))
                self._out_lines += 1


# --- helpers ---------------------------------------------------------------


# Matches both legacy `[pai]` and new `[pai:<slug>]` prefixes.
_PAI_PREFIX = re.compile(r"^\[pai(?::(?P<pid>[^\]]+))?\]")


def _short_when(when: str) -> str:
    """Trim ISO timestamps to MM-DD HH:MM; leave cron schedules alone."""
    if not when:
        return ""
    try:
        dt = datetime.fromisoformat(when)
        return dt.strftime("%m-%d %H:%M")
    except ValueError:
        return when
