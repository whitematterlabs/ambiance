"""First-class `read` tool — file contents (and images) without `cat`.

Text output is head-truncated to 2000 lines / 50KB with pi-style
continuation footers (`Use offset=N to continue`). Image files come back
as a `![](path)` markdown ref; the tool-loop's `expand_image_refs` pass
turns that into an inline image block for the model.
"""

from __future__ import annotations

import errno as _errno
from typing import Optional

from ._file_common import FileToolResult, resolve_tool_path
from .image_refs import _EXT_TO_MEDIA
from .truncate import DEFAULT_MAX_BYTES, format_size, truncate_head

TOOL_NAME = "read"
TOOL_DESCRIPTION = (
    "Read the contents of a file. Supports text files and images (png, jpg, "
    "gif, webp). Images are attached to the conversation. For text files, "
    "output is truncated to 2000 lines or 50KB (whichever is hit first). Use "
    "offset/limit for large files; when you need the full file, continue with "
    "offset until complete. Paths: absolute FHS paths (`/etc/`, `/home/`, "
    "`/tmp/`, ...) refer to PAI's world; a path outside the FHS must be a "
    "real host path; relative paths resolve against your home. Use `read` to "
    "examine files instead of `cat` or `sed`."
)

TOOL_SCHEMA = {
    "name": TOOL_NAME,
    "description": TOOL_DESCRIPTION,
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path to the file to read (absolute or relative)",
            },
            "offset": {
                "type": "integer",
                "description": "Line number to start reading from (1-indexed)",
            },
            "limit": {
                "type": "integer",
                "description": "Maximum number of lines to read",
            },
        },
        "required": ["path"],
    },
}


def _errname(e: OSError) -> str:
    return _errno.errorcode.get(e.errno, "") or str(e)


def _to_int(value: object) -> Optional[int]:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def run(tool_input: dict, env: Optional[dict] = None) -> FileToolResult:
    path_raw = tool_input.get("path")
    if not path_raw or not isinstance(path_raw, str):
        return FileToolResult("read tool: `path` is required", is_error=True)
    target = resolve_tool_path(path_raw, env)

    media = _EXT_TO_MEDIA.get(target.suffix.lower())
    if media:
        if not target.is_file():
            return FileToolResult(
                f"Could not read file: {path_raw}. ENOENT.", is_error=True
            )
        return FileToolResult(f"Read image file [{media}]\n![]({target})")

    try:
        content = target.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return FileToolResult(
            f"Could not read file: {path_raw}. {_errname(e)}.", is_error=True
        )

    offset = _to_int(tool_input.get("offset"))
    limit = _to_int(tool_input.get("limit"))

    all_lines = content.split("\n")
    total_file_lines = len(all_lines)
    start_line = max(0, offset - 1) if offset else 0
    start_disp = start_line + 1
    if start_line >= total_file_lines:
        return FileToolResult(
            f"Offset {offset} is beyond end of file ({total_file_lines} lines total)",
            is_error=True,
        )

    user_limited: Optional[int] = None
    if limit is not None:
        end_line = min(start_line + limit, total_file_lines)
        selected = "\n".join(all_lines[start_line:end_line])
        user_limited = end_line - start_line
    else:
        selected = "\n".join(all_lines[start_line:])

    t = truncate_head(selected)
    if t.first_line_exceeds_limit:
        first_line_size = format_size(len(all_lines[start_line].encode("utf-8")))
        return FileToolResult(
            f"[Line {start_disp} is {first_line_size}, exceeds "
            f"{format_size(DEFAULT_MAX_BYTES)} limit. Use bash: "
            f"sed -n '{start_disp}p' {path_raw} | head -c {DEFAULT_MAX_BYTES}]"
        )
    if t.truncated:
        end_disp = start_disp + t.output_lines - 1
        next_offset = end_disp + 1
        if t.truncated_by == "lines":
            footer = (
                f"\n\n[Showing lines {start_disp}-{end_disp} of "
                f"{total_file_lines}. Use offset={next_offset} to continue.]"
            )
        else:
            footer = (
                f"\n\n[Showing lines {start_disp}-{end_disp} of "
                f"{total_file_lines} ({format_size(DEFAULT_MAX_BYTES)} limit). "
                f"Use offset={next_offset} to continue.]"
            )
        return FileToolResult(t.content + footer)
    if user_limited is not None and start_line + user_limited < total_file_lines:
        remaining = total_file_lines - (start_line + user_limited)
        next_offset = start_line + user_limited + 1
        return FileToolResult(
            f"{t.content}\n\n[{remaining} more lines in file. "
            f"Use offset={next_offset} to continue.]"
        )
    return FileToolResult(t.content)
