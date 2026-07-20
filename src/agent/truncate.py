"""Line/byte-budget truncation for tool outputs (ported from pi's truncate.ts).

Two independent limits — whichever is hit first wins: 2000 lines or 50KB.
`truncate_head` keeps the start (file reads); `truncate_tail` keeps the end
(bash/shell output, where errors and final results live). Neither returns
partial lines, except the tail's giant-single-line edge case.

`cap_tail_for_model` is the bash/shell entry point: it tail-truncates the
model-bound copy of a tool result and, when truncation happened, spills the
full text to the system temp dir and cites the path in the footer so the
agent can `tail`/`grep` the rest itself.
"""

from __future__ import annotations

import re
import secrets
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

DEFAULT_MAX_LINES = 2000
DEFAULT_MAX_BYTES = 50 * 1024  # 50KB


@dataclass
class Truncation:
    content: str
    truncated: bool
    truncated_by: Optional[str]  # "lines" | "bytes" | None
    total_lines: int
    total_bytes: int
    output_lines: int
    output_bytes: int
    last_line_partial: bool = False
    first_line_exceeds_limit: bool = False


def _split_lines_for_counting(content: str) -> list[str]:
    """Split into lines, not counting a trailing newline as an extra line."""
    if not content:
        return []
    lines = content.split("\n")
    if content.endswith("\n"):
        lines.pop()
    return lines


def format_size(n: int) -> str:
    if n < 1024:
        return f"{n}B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f}KB"
    return f"{n / (1024 * 1024):.1f}MB"


def _passthrough(content: str, total_lines: int, total_bytes: int) -> Truncation:
    return Truncation(
        content=content,
        truncated=False,
        truncated_by=None,
        total_lines=total_lines,
        total_bytes=total_bytes,
        output_lines=total_lines,
        output_bytes=total_bytes,
    )


def truncate_head(
    content: str,
    *,
    max_lines: int = DEFAULT_MAX_LINES,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> Truncation:
    """Keep the first N lines/bytes. Never returns partial lines; a first
    line alone over the byte budget yields empty content with
    `first_line_exceeds_limit` set."""
    total_bytes = len(content.encode("utf-8"))
    lines = _split_lines_for_counting(content)
    total_lines = len(lines)

    if total_lines <= max_lines and total_bytes <= max_bytes:
        return _passthrough(content, total_lines, total_bytes)

    if len(lines[0].encode("utf-8")) > max_bytes:
        return Truncation(
            content="",
            truncated=True,
            truncated_by="bytes",
            total_lines=total_lines,
            total_bytes=total_bytes,
            output_lines=0,
            output_bytes=0,
            first_line_exceeds_limit=True,
        )

    out: list[str] = []
    out_bytes = 0
    truncated_by = "lines"
    for i, line in enumerate(lines[:max_lines]):
        line_bytes = len(line.encode("utf-8")) + (1 if i > 0 else 0)  # +1 newline
        if out_bytes + line_bytes > max_bytes:
            truncated_by = "bytes"
            break
        out.append(line)
        out_bytes += line_bytes
    if len(out) >= max_lines and out_bytes <= max_bytes:
        truncated_by = "lines"

    out_content = "\n".join(out)
    return Truncation(
        content=out_content,
        truncated=True,
        truncated_by=truncated_by,
        total_lines=total_lines,
        total_bytes=total_bytes,
        output_lines=len(out),
        output_bytes=len(out_content.encode("utf-8")),
    )


def truncate_tail(
    content: str,
    *,
    max_lines: int = DEFAULT_MAX_LINES,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> Truncation:
    """Keep the last N lines/bytes. May return a partial first line only when
    the very last line alone exceeds the byte budget."""
    total_bytes = len(content.encode("utf-8"))
    lines = _split_lines_for_counting(content)
    total_lines = len(lines)

    if total_lines <= max_lines and total_bytes <= max_bytes:
        return _passthrough(content, total_lines, total_bytes)

    out: list[str] = []
    out_bytes = 0
    truncated_by = "lines"
    last_line_partial = False
    for line in reversed(lines):
        if len(out) >= max_lines:
            break
        line_bytes = len(line.encode("utf-8")) + (1 if out else 0)  # +1 newline
        if out_bytes + line_bytes > max_bytes:
            truncated_by = "bytes"
            if not out:
                partial = _truncate_string_to_bytes_from_end(line, max_bytes)
                out.insert(0, partial)
                out_bytes = len(partial.encode("utf-8"))
                last_line_partial = True
            break
        out.insert(0, line)
        out_bytes += line_bytes
    if len(out) >= max_lines and out_bytes <= max_bytes:
        truncated_by = "lines"

    out_content = "\n".join(out)
    return Truncation(
        content=out_content,
        truncated=True,
        truncated_by=truncated_by,
        total_lines=total_lines,
        total_bytes=total_bytes,
        output_lines=len(out),
        output_bytes=len(out_content.encode("utf-8")),
        last_line_partial=last_line_partial,
    )


def _truncate_string_to_bytes_from_end(s: str, max_bytes: int) -> str:
    """Keep the last `max_bytes` bytes of `s`, snapped to a UTF-8 boundary."""
    buf = s.encode("utf-8")
    if len(buf) <= max_bytes:
        return s
    start = len(buf) - max_bytes
    # Walk forward past continuation bytes (10xxxxxx) to a character start.
    while start < len(buf) and (buf[start] & 0xC0) == 0x80:
        start += 1
    return buf[start:].decode("utf-8")


def spill(text: str, *, prefix: str) -> str:
    """Write `text` to `<tmpdir>/<prefix>-<8hex>.log`; return the path.
    The OS owns temp-dir cleanup."""
    name = f"{prefix}-{secrets.token_hex(4)}.log"
    path = Path(tempfile.gettempdir()) / name
    path.write_text(text, encoding="utf-8")
    return str(path)


def cap_tail_for_model(rendered: str, *, tool: str, slug: str = "") -> str:
    """Model-bound copy of a bash/shell result: last 2000 lines / 50KB.

    When truncated, the full text is spilled to a temp file cited in the
    footer, so the agent can read the rest with `tail`/`grep`. Untruncated
    text passes through with no file written. `slug` only flavors the spill
    filename; it is optional (one process = one member in v4)."""
    t = truncate_tail(rendered)
    if not t.truncated:
        return rendered
    prefix = tool
    if slug:
        prefix += "-" + re.sub(r"[^A-Za-z0-9_-]+", "-", slug)
    path = spill(rendered, prefix=prefix)
    end_line = t.total_lines
    start_line = t.total_lines - t.output_lines + 1
    if t.last_line_partial:
        lines = _split_lines_for_counting(rendered)
        full_line_size = format_size(len(lines[-1].encode("utf-8")))
        footer = (
            f"\n\n[Showing last {format_size(t.output_bytes)} of line {end_line} "
            f"(line is {full_line_size}). Full output: {path}]"
        )
    elif t.truncated_by == "lines":
        footer = (
            f"\n\n[Showing lines {start_line}-{end_line} of {t.total_lines}. "
            f"Full output: {path}]"
        )
    else:
        footer = (
            f"\n\n[Showing lines {start_line}-{end_line} of {t.total_lines} "
            f"({format_size(DEFAULT_MAX_BYTES)} limit). Full output: {path}]"
        )
    return t.content + footer
