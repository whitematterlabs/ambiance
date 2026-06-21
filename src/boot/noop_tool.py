"""Terminal no-op tool for turns that require no action or reply."""

from __future__ import annotations


TOOL_NAME = "NOOP"
TOOL_RESULT = "NOOP: no action taken."

TOOL_SCHEMA = {
    "name": TOOL_NAME,
    "description": (
        "Use this when the event requires no filesystem action, tool work, "
        "delegation, or owner-facing reply. This is a terminal choice: call "
        "NOOP instead of writing filler text such as 'quiet', 'nothing to do', "
        "or 'doing nothing'."
    ),
    "input_schema": {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
}
