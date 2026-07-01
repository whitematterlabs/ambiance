"""Terminal stand-down tool for turns that require no action or reply."""

from __future__ import annotations


TOOL_NAME = "stand_down"
TOOL_RESULT = "stand_down: turn ended, no action taken."

TOOL_SCHEMA = {
    "name": TOOL_NAME,
    "description": (
        "Required terminal tool for quiet turns. Call this to end the turn "
        "when the event needs no filesystem action, no further tool work, no "
        "delegation, and no owner-facing reply. This is a control action that "
        "ends your turn silently — never write the tool's name, and never "
        "write filler text such as 'quiet', 'nothing to do', 'no update', or "
        "'doing nothing' in its place."
    ),
    "input_schema": {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
}

# The model occasionally expresses a quiet turn as assistant prose instead of
# calling this tool — it types the sentinel ("stand_down", or the old "NOOP")
# or one of the forbidden filler phrases as its final text. Those leak to the
# owner surface as a bogus one-word message. Canonicalize every such form back
# to "no reply" whichever channel it arrives on. "noop"/"no-op" are the tool's
# historical name, kept here so pre-rename habit is still absorbed.
_SENTINEL_TEXTS = frozenset({
    "stand_down",
    "stand down",
    "standdown",
    "noop",
    "no-op",
    "quiet",
    "nothing to do",
    "no update",
    "doing nothing",
})


def is_sentinel_text(text: str) -> bool:
    """True when a no-tool-use final reply is really a quiet-turn sentinel the
    model typed as prose instead of calling the tool."""
    normalized = text.strip().lower().rstrip(".!").strip()
    return normalized in _SENTINEL_TEXTS
