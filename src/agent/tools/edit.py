"""First-class `edit` tool — exact-text replacement (ported from pi's edit.ts).

Every `edits[].oldText` is matched against the ORIGINAL file content (not
incrementally), must be unique, and must not overlap any other edit.
Replacements are applied in reverse offset order so positions stay stable,
then written back in one atomic write. BOM and CRLF are stripped/normalized
before matching and restored on write. No fuzzy matching (v1).
"""

from __future__ import annotations

import errno as _errno
import json
from typing import Optional

from ._file_common import FileToolResult, atomic_write, resolve_tool_path

TOOL_NAME = "edit"
TOOL_DESCRIPTION = (
    "Edit a single file using exact text replacement. Every edits[].oldText "
    "must match a unique, non-overlapping region of the original file. If two "
    "changes affect the same block or nearby lines, merge them into one edit "
    "instead of emitting overlapping edits. Do not include large unchanged "
    "regions just to connect distant changes. Paths resolve like `read`: "
    "absolute, or relative to your home."
)

TOOL_SCHEMA = {
    "name": TOOL_NAME,
    "description": TOOL_DESCRIPTION,
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path to the file to edit (absolute or relative)",
            },
            "edits": {
                "type": "array",
                "description": (
                    "One or more targeted replacements. Each edit is matched "
                    "against the original file, not incrementally. Do not "
                    "include overlapping or nested edits. If two changes touch "
                    "the same block or nearby lines, merge them into one edit "
                    "instead."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "oldText": {
                            "type": "string",
                            "description": (
                                "Exact text for one targeted replacement. It "
                                "must be unique in the original file and must "
                                "not overlap with any other edits[].oldText in "
                                "the same call."
                            ),
                        },
                        "newText": {
                            "type": "string",
                            "description": "Replacement text for this targeted edit.",
                        },
                    },
                    "required": ["oldText", "newText"],
                },
            },
        },
        "required": ["path", "edits"],
    },
}


class EditError(Exception):
    pass


def _errname(e: OSError) -> str:
    return _errno.errorcode.get(e.errno, "") or str(e)


def _detect_line_ending(content: str) -> str:
    crlf_idx = content.find("\r\n")
    lf_idx = content.find("\n")
    if lf_idx == -1 or crlf_idx == -1:
        return "\n"
    return "\r\n" if crlf_idx < lf_idx else "\n"


def _normalize_to_lf(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _restore_line_endings(text: str, ending: str) -> str:
    return text.replace("\n", "\r\n") if ending == "\r\n" else text


_BOM = "\ufeff"


def _strip_bom(content: str) -> tuple[str, str]:
    if content.startswith(_BOM):
        return _BOM, content[1:]
    return "", content


def _prepare_edits(tool_input: dict) -> list[dict]:
    """Input hardening: `edits` sent as a JSON string is parsed; legacy
    top-level oldText/newText is treated as one edit."""
    edits = tool_input.get("edits")
    if isinstance(edits, str):
        try:
            parsed = json.loads(edits)
            if isinstance(parsed, list):
                edits = parsed
        except (ValueError, TypeError):
            pass
    edits = list(edits) if isinstance(edits, list) else []
    if isinstance(tool_input.get("oldText"), str) and isinstance(
        tool_input.get("newText"), str
    ):
        edits.append(
            {"oldText": tool_input["oldText"], "newText": tool_input["newText"]}
        )
    return edits


def _apply_edits(content: str, edits: list[tuple[str, str]], path: str) -> str:
    """Apply exact-text replacements, all matched against `content`."""
    total = len(edits)
    matched: list[tuple[int, int, str, int]] = []  # (offset, len, newText, i)
    for i, (old, new) in enumerate(edits):
        if not old:
            if total == 1:
                raise EditError(f"oldText must not be empty in {path}.")
            raise EditError(f"edits[{i}].oldText must not be empty in {path}.")
        count = content.count(old)
        if count == 0:
            if total == 1:
                raise EditError(
                    f"Could not find the exact text in {path}. The old text "
                    f"must match exactly including all whitespace and newlines."
                )
            raise EditError(
                f"Could not find edits[{i}] in {path}. The oldText must match "
                f"exactly including all whitespace and newlines."
            )
        if count > 1:
            if total == 1:
                raise EditError(
                    f"Found {count} occurrences of the text in {path}. The "
                    f"text must be unique. Please provide more context to "
                    f"make it unique."
                )
            raise EditError(
                f"Found {count} occurrences of edits[{i}] in {path}. Each "
                f"oldText must be unique. Please provide more context to make "
                f"it unique."
            )
        matched.append((content.index(old), len(old), new, i))

    matched.sort(key=lambda m: m[0])
    for prev, cur in zip(matched, matched[1:]):
        if prev[0] + prev[1] > cur[0]:
            raise EditError(
                f"edits[{prev[3]}] and edits[{cur[3]}] overlap in {path}. "
                f"Merge them into one edit or target disjoint regions."
            )

    new_content = content
    for offset, length, new, _ in reversed(matched):
        new_content = new_content[:offset] + new + new_content[offset + length:]

    if new_content == content:
        if total == 1:
            raise EditError(
                f"No changes made to {path}. The replacement produced "
                f"identical content. This might indicate an issue with "
                f"special characters or the text not existing as expected."
            )
        raise EditError(
            f"No changes made to {path}. The replacements produced identical "
            f"content."
        )
    return new_content


def run(tool_input: dict, env: Optional[dict] = None) -> FileToolResult:
    path_raw = tool_input.get("path")
    if not path_raw or not isinstance(path_raw, str):
        return FileToolResult("edit tool: `path` is required", is_error=True)

    edits = _prepare_edits(tool_input)
    if not edits or not all(
        isinstance(e, dict)
        and isinstance(e.get("oldText"), str)
        and isinstance(e.get("newText"), str)
        for e in edits
    ):
        return FileToolResult(
            "edit tool: `edits` must be a non-empty array of "
            "{oldText, newText} objects",
            is_error=True,
        )

    target = resolve_tool_path(path_raw, env)
    try:
        # newline="" keeps CRLF intact — universal-newline mode would
        # translate it to LF before _detect_line_ending can see it.
        raw_content = target.read_text(encoding="utf-8", newline="")
    except (OSError, UnicodeDecodeError) as e:
        detail = _errname(e) if isinstance(e, OSError) else str(e)
        return FileToolResult(
            f"Could not edit file: {path_raw}. {detail}.", is_error=True
        )

    bom, content = _strip_bom(raw_content)
    ending = _detect_line_ending(content)
    normalized = _normalize_to_lf(content)
    norm_edits = [
        (_normalize_to_lf(e["oldText"]), _normalize_to_lf(e["newText"]))
        for e in edits
    ]
    try:
        new_content = _apply_edits(normalized, norm_edits, path_raw)
    except EditError as e:
        return FileToolResult(str(e), is_error=True)

    final = bom + _restore_line_endings(new_content, ending)
    try:
        atomic_write(target, final)
    except OSError as e:
        return FileToolResult(
            f"Could not edit file: {path_raw}. {_errname(e)}.", is_error=True
        )
    return FileToolResult(
        f"Successfully replaced {len(edits)} block(s) in {path_raw}."
    )
