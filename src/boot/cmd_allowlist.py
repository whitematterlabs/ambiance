"""Token-prefix allowlist matching for owner-gated shell commands.

A rule is a command prefix ("git status", "rg"): a command matches when its
leading tokens equal the rule's tokens exactly. Compound commands are split
on unquoted separators (`;`, `|`, `&`, newlines) and EVERY segment must
match some rule — `ls && rm -rf /` is not an `ls`.

Deliberately conservative: anything this module can't confidently reason
about (command/process substitution, subshells, brace groups, unclosed
quotes) never matches, which in ask mode means it goes to the owner. A
false negative costs one approval click; a false positive runs an
unapproved command.
"""

from __future__ import annotations

import shlex

# Constructs that nest command execution inside a word: prefix matching is
# meaningless in their presence (`ls $(rm -rf ~)` is not an `ls`).
_SUBSTITUTION_MARKERS = ("$(", "`", "<(", ">(")


def _split_segments(command: str) -> list[str] | None:
    """Split on unquoted command separators. Returns None when the command
    can't be confidently segmented (unclosed quote, subshell/brace group) —
    the caller treats None as no-match."""
    segments: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    escaped = False
    i, n = 0, len(command)
    while i < n:
        ch = command[i]
        if escaped:
            buf.append(ch)
            escaped = False
        elif ch == "\\":
            buf.append(ch)
            escaped = True
        elif quote:
            buf.append(ch)
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
            buf.append(ch)
        elif ch == "&" and (
            (i > 0 and command[i - 1] == ">") or (i + 1 < n and command[i + 1] == ">")
        ):
            # `2>&1` / `&>file` — a redirection, not a separator.
            buf.append(ch)
        elif ch in ";|&\n":
            segments.append("".join(buf))
            buf = []
        elif ch in "(){}":
            return None
        else:
            buf.append(ch)
        i += 1
    if quote or escaped:
        return None
    segments.append("".join(buf))
    out = [s.strip() for s in segments]
    return [s for s in out if s]


def command_allowed(command: str, rules: list[str]) -> bool:
    """True iff every segment of `command` prefix-matches some rule."""
    if not command or not command.strip():
        return False
    if any(m in command for m in _SUBSTITUTION_MARKERS):
        return False
    parsed_rules: list[list[str]] = []
    for rule in rules or []:
        if not isinstance(rule, str) or not rule.strip():
            continue
        try:
            toks = shlex.split(rule)
        except ValueError:
            continue
        if toks:
            parsed_rules.append(toks)
    if not parsed_rules:
        return False
    segments = _split_segments(command)
    if not segments:
        return False
    for seg in segments:
        try:
            toks = shlex.split(seg)
        except ValueError:
            return False
        if not toks:
            return False
        if not any(toks[: len(r)] == r for r in parsed_rules):
            return False
    return True
