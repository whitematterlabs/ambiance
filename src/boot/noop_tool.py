"""Terminal do-nothing tool for turns that require no action or reply."""

from __future__ import annotations


TOOL_NAME = "do_nothing"
TOOL_RESULT = "do_nothing: turn ended, no action taken."

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
# calling this tool — it types the sentinel ("do_nothing", or an older name
# like "stand_down"/"NOOP") or one of the forbidden filler phrases as its final
# text. Those leak to the owner surface as a bogus one-word message. Canonicalize
# every such form back to "no reply" whichever channel it arrives on.
# "stand_down"/"noop"/"no-op" are the tool's historical names, kept here so
# pre-rename habit is still absorbed.
_SENTINEL_TEXTS = frozenset({
    "do_nothing",
    "do nothing",
    "donothing",
    "stand_down",
    "stand down",
    "standdown",
    "noop",
    "no-op",
    "quiet",
    "nothing to do",
    "no update",
    "doing nothing",
    "no reply needed",
    "no reply",
    "no response needed",
    "no action needed",
    "nothing to report",
})

# Decoration the model wraps sentinels in ("*(no reply needed)*",
# "`do_nothing`", "\"quiet\"") — stripped before matching.
_SENTINEL_WRAPPING = "*_`~\"'()[]{}"


def is_sentinel_text(text: str) -> bool:
    """True when a no-tool-use final reply is really a quiet-turn sentinel the
    model typed as prose instead of calling the tool."""
    normalized = text.strip().lower().rstrip(".!").strip()
    normalized = normalized.strip(_SENTINEL_WRAPPING).strip().rstrip(".!").strip()
    return normalized in _SENTINEL_TEXTS
