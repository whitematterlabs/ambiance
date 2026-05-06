"""Pre-handoff debugger pass.

Wraps a subagent turn: snapshot mtimes of watched paths before the turn,
diff after, and if any files were touched, ask a reviewer LLM to re-read
them and either say `LGTM` or return rewritten contents. Apply the
rewrites in place, scope-checked against the touched set.

Configured per-subagent via the `debugger:` block on the bundle's
`package.yaml`. Absent block → this module is never called and
behavior is unchanged.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from . import llm
from .processes import ProcessNotFound, append_log


_REVIEW_SYSTEM = (
    "You are a strict code reviewer running as a final pass after a coder "
    "agent finished its turn. You will receive: (1) the original task, "
    "(2) the coder's final reply, (3) the current contents of every file "
    "the coder touched. Re-read the files and look for clear bugs: wrong "
    "logic, off-by-one errors, swapped operators, missing edge cases, "
    "obvious typos that change behavior. Do NOT restyle, rename, refactor, "
    "or add features.\n\n"
    "Respond with EXACTLY one of:\n"
    "  - The literal text `LGTM` (no other characters) if the files look correct.\n"
    "  - A JSON object of shape "
    "`{\"files\": [{\"path\": \"<same path you were shown>\", "
    "\"content\": \"<full new file contents>\"}]}` "
    "containing only the files you want to rewrite. Provide complete file "
    "contents, NOT diffs. Only include files from the set you were shown.\n\n"
    "No prose, no markdown fences around the JSON, no explanation. Either "
    "`LGTM` or the JSON object — nothing else."
)


def snapshot(
    roots: list[Path],
    excludes: list[Path],
) -> dict[str, float]:
    """Walk `roots`, skipping any path under `excludes`, and return a
    map of absolute-path-string → mtime for every regular file found.

    Both `roots` and `excludes` must be absolute paths. Symlinks are
    followed for directories but the mtime recorded is the link target's.
    Missing roots are silently skipped.
    """
    out: dict[str, float] = {}
    excl_strs = [str(e) for e in excludes]

    def _is_excluded(path_str: str) -> bool:
        for ex in excl_strs:
            if path_str == ex or path_str.startswith(ex + os.sep):
                return True
        return False

    for root in roots:
        if not root.exists():
            continue
        if root.is_file():
            if not _is_excluded(str(root)):
                try:
                    out[str(root)] = root.stat().st_mtime
                except OSError:
                    pass
            continue
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            if _is_excluded(dirpath):
                dirnames[:] = []
                continue
            # Prune excluded subdirs in-place so os.walk skips them.
            dirnames[:] = [
                d for d in dirnames
                if not _is_excluded(os.path.join(dirpath, d))
            ]
            for fn in filenames:
                fp = os.path.join(dirpath, fn)
                if _is_excluded(fp):
                    continue
                try:
                    out[fp] = os.stat(fp).st_mtime
                except OSError:
                    pass
    return out


def _resolve_paths(
    pai_root: Path,
    rel_paths: list[str],
) -> list[Path]:
    return [(pai_root / p).resolve() for p in rel_paths or []]


def _diff(
    pre: dict[str, float],
    post: dict[str, float],
) -> set[str]:
    """Return the set of absolute paths present in `post` whose mtime
    differs from `pre` (new or modified). Deletions (in pre, missing in
    post) are excluded — there's nothing to review."""
    touched: set[str] = set()
    for path, mtime in post.items():
        if pre.get(path) != mtime:
            touched.add(path)
    return touched


def _first_user_text(history: list[dict]) -> str:
    for msg in history:
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(block.get("text") or "")
                elif isinstance(block, dict) and block.get("type") == "tool_result":
                    # Skip tool results — these are tool turns, not user text.
                    continue
            text = "\n".join(p for p in parts if p).strip()
            if text:
                return text
    return ""


def _last_assistant_text(history: list[dict]) -> str:
    for msg in reversed(history):
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(block.get("text") or "")
            text = "\n".join(p for p in parts if p).strip()
            if text:
                return text
    return ""


def _build_user_prompt(
    history: list[dict],
    touched_files: dict[str, str],
) -> str:
    task = _first_user_text(history) or "(no original task captured)"
    reply = _last_assistant_text(history) or "(no final reply captured)"
    parts = [
        "## Original task",
        task,
        "",
        "## Coder's final reply",
        reply,
        "",
        "## Files coder touched (current contents)",
    ]
    for path, content in touched_files.items():
        parts.append(f"\n### {path}")
        parts.append("```")
        parts.append(content)
        parts.append("```")
    return "\n".join(parts)


def _parse_response(text: str) -> Optional[list[dict]]:
    """Return None for LGTM, a list of {path, content} dicts for a
    rewrite, or raise ValueError on unparseable output."""
    stripped = text.strip()
    if stripped == "LGTM":
        return None
    # Strip optional ```json fences if the model added them despite instruction.
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        if stripped.startswith("json"):
            stripped = stripped[4:].lstrip()
    try:
        obj = json.loads(stripped)
    except json.JSONDecodeError as e:
        raise ValueError(f"reviewer returned non-JSON: {e}") from e
    if not isinstance(obj, dict) or not isinstance(obj.get("files"), list):
        raise ValueError("reviewer JSON missing 'files' list")
    out = []
    for entry in obj["files"]:
        if not isinstance(entry, dict):
            raise ValueError("file entry not an object")
        path = entry.get("path")
        content = entry.get("content")
        if not isinstance(path, str) or not isinstance(content, str):
            raise ValueError("file entry missing path/content")
        out.append({"path": path, "content": content})
    return out


def _apply(
    rewrites: list[dict],
    touched_set: set[str],
    pai_root: Path,
    max_lines: int,
) -> tuple[list[str], list[str]]:
    """Apply rewrites whose path resolves into the touched set.

    Returns (applied_paths, warnings). Out-of-scope paths produce a
    warning and are skipped. A rewrite that exceeds max_lines produces
    a warning but is applied anyway (auditable, not gated)."""
    applied: list[str] = []
    warnings: list[str] = []
    for entry in rewrites:
        rel = entry["path"]
        new_content = entry["content"]
        target = (pai_root / rel).resolve()
        target_str = str(target)
        if target_str not in touched_set:
            warnings.append(f"rejected out-of-scope path: {rel}")
            continue
        try:
            existing = target.read_text() if target.exists() else ""
        except OSError:
            existing = ""
        old_lines = existing.splitlines()
        new_lines = new_content.splitlines()
        delta = abs(len(new_lines) - len(old_lines)) + sum(
            1 for a, b in zip(old_lines, new_lines) if a != b
        )
        if delta > max_lines:
            warnings.append(
                f"large rewrite ({delta} lines changed > max_lines={max_lines}): {rel}"
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(new_content)
        applied.append(rel)
    return applied, warnings


async def _call_reviewer(
    provider: str,
    model: str,
    system: str,
    user: str,
) -> str:
    client, resolved_model, extra_body = llm._resolve(provider, model)
    response = await client.messages.create(
        model=resolved_model,
        max_tokens=8192,
        system=system,
        messages=[{"role": "user", "content": user}],
        extra_body=extra_body,
    )
    parts = []
    for block in response.content:
        # Both SDK objects and dicts may appear depending on extra_body shape.
        btype = getattr(block, "type", None) or (
            block.get("type") if isinstance(block, dict) else None
        )
        if btype != "text":
            continue
        btext = getattr(block, "text", None) or (
            block.get("text") if isinstance(block, dict) else ""
        )
        if btext:
            parts.append(btext)
    return "\n".join(parts).strip()


async def review(
    pai_slug: str,
    pai_root: Path,
    config: dict,
    history: list[dict],
    pre_snapshot: dict[str, float],
) -> None:
    """Run the post-turn review pass. Never raises — all failures are logged."""
    try:
        watch_paths = _resolve_paths(pai_root, config.get("watch_paths") or [])
        excludes = _resolve_paths(pai_root, config.get("exclude") or [])
        max_lines = int(config.get("max_lines") or 50)
        provider = config.get("provider")
        model = config.get("model")

        post = snapshot(watch_paths, excludes)
        touched_set = _diff(pre_snapshot, post)
        if not touched_set:
            _log(pai_slug, "[debugger] no files touched — skipping review")
            return

        touched_files: dict[str, str] = {}
        for abs_path in sorted(touched_set):
            try:
                rel = str(Path(abs_path).resolve().relative_to(pai_root.resolve()))
            except ValueError:
                rel = abs_path
            try:
                touched_files[rel] = Path(abs_path).read_text()
            except OSError as e:
                _log(pai_slug, f"[debugger] could not read {rel}: {e!r}")

        if not touched_files:
            return

        user_prompt = _build_user_prompt(history, touched_files)
        text = await _call_reviewer(provider, model, _REVIEW_SYSTEM, user_prompt)

        try:
            rewrites = _parse_response(text)
        except ValueError as e:
            _log(pai_slug, f"[debugger] unparseable reviewer output — {e}")
            return

        if rewrites is None:
            _log(pai_slug, "[debugger] LGTM")
            return

        # Scope check uses absolute resolved paths.
        applied, warnings = _apply(rewrites, touched_set, pai_root, max_lines)
        for w in warnings:
            _log(pai_slug, f"[debugger] {w}")
        if applied:
            _log(pai_slug, f"[debugger] applied: {', '.join(applied)}")
        else:
            _log(pai_slug, "[debugger] reviewer returned rewrites but none applied")
    except Exception as e:
        _log(pai_slug, f"[debugger] failed — {e!r}")


def _log(pai_slug: str, msg: str) -> None:
    try:
        append_log(pai_slug, msg)
    except ProcessNotFound:
        pass
