"""First-class `write` tool — create or fully overwrite a file atomically."""

from __future__ import annotations

import errno as _errno
from typing import Optional

from ._file_common import FileToolResult, atomic_write, resolve_tool_path

TOOL_NAME = "write"
TOOL_DESCRIPTION = (
    "Write content to a file. Creates the file if it doesn't exist, "
    "overwrites if it does. Automatically creates parent directories. Use "
    "write only for new files or complete rewrites; use `edit` for targeted "
    "changes. Paths resolve like `read`: absolute, or relative to your home."
)

TOOL_SCHEMA = {
    "name": TOOL_NAME,
    "description": TOOL_DESCRIPTION,
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path to the file to write (absolute or relative)",
            },
            "content": {
                "type": "string",
                "description": "Content to write to the file",
            },
        },
        "required": ["path", "content"],
    },
}


def _errname(e: OSError) -> str:
    return _errno.errorcode.get(e.errno, "") or str(e)


def run(tool_input: dict, env: Optional[dict] = None) -> FileToolResult:
    path_raw = tool_input.get("path")
    if not path_raw or not isinstance(path_raw, str):
        return FileToolResult("write tool: `path` is required", is_error=True)
    content = tool_input.get("content")
    if not isinstance(content, str):
        return FileToolResult("write tool: `content` is required", is_error=True)

    target = resolve_tool_path(path_raw, env)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(target, content)
    except OSError as e:
        return FileToolResult(
            f"Could not write file: {path_raw}. {_errname(e)}.", is_error=True
        )
    n = len(content.encode("utf-8"))
    return FileToolResult(f"Successfully wrote {n} bytes to {path_raw}")
