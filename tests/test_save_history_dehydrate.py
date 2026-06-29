"""`_save_history` must never write base64 image data to the transcript.

Base64 image blocks are hydrated for one API call (see image_refs) but the same
in-memory `messages` list is what gets persisted. Without dehydration a single
screenshot is ~400x its image-token cost when re-read as text, which is how a
4.9 MB messages.jsonl blew a 1M-token window. The on-disk copy must carry a
text placeholder, never the base64.
"""

from __future__ import annotations

import base64
from pathlib import Path

from boot import nudge as N

# 1x1 PNG.
_PNG_BYTES = base64.b64decode(
    b"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)
_B64 = base64.standard_b64encode(_PNG_BYTES).decode("ascii")


def test_save_history_strips_base64(tmp_path: Path) -> None:
    path = tmp_path / "messages.jsonl"
    messages = [
        {"role": "user", "content": "hi"},
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "t-1",
                    "content": [
                        {"type": "text", "text": "shot:"},
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": _B64,
                            },
                        },
                    ],
                }
            ],
        },
    ]

    N._save_history(path, messages)
    text = path.read_text()

    assert _B64 not in text
    assert '"data"' not in text
    assert "[image elided from history:" in text
    # In-memory list is untouched.
    assert messages[1]["content"][0]["content"][1]["source"]["data"] == _B64
