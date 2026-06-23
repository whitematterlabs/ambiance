"""Terminal no-op tool for turns that require no action or reply."""

from __future__ import annotations


TOOL_NAME = "NOOP"
TOOL_RESULT = "NOOP: no action taken."

TOOL_SCHEMA = {
    "name": TOOL_NAME,
    "description": (
        "Required terminal tool for quiet turns. Use this when the event "
        "needs no further filesystem action, no further tool work, no "
        "delegation, and no owner-facing reply. Call NOOP instead of writing "
        "filler text such as 'quiet', 'nothing to do', 'no update', or "
        "'doing nothing'."
    ),
    "input_schema": {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
}
