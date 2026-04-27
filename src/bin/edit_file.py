#!/usr/bin/env python
"""edit_file — exact-string, atomic, content-addressed file edit.

Mirrors Claude Code's Edit tool: literal old/new, uniqueness-required,
atomic write, unified-diff receipt on stdout.
"""

from __future__ import annotations

import argparse
import difflib
import os
import sys
import tempfile
from pathlib import Path


def _read_input(name: str, file_arg: str | None, stdin_flag: bool) -> str:
    if file_arg and stdin_flag:
        print(f"error: --{name}-file and --{name}-stdin are mutually exclusive", file=sys.stderr)
        raise SystemExit(5)
    if file_arg:
        return Path(file_arg).read_text()
    if stdin_flag:
        return sys.stdin.read()
    print(f"error: must pass one of --{name}-file or --{name}-stdin", file=sys.stderr)
    raise SystemExit(5)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="edit_file",
        description="Atomic exact-string file edit. Symlinks are resolved and the target is edited.",
    )
    p.add_argument("path", help="file to edit (symlinks resolved)")
    p.add_argument("--old-file", help="read old string from this file")
    p.add_argument("--old-stdin", action="store_true", help="read old string from stdin")
    p.add_argument("--new-file", help="read new string from this file")
    p.add_argument("--new-stdin", action="store_true", help="read new string from stdin")
    p.add_argument(
        "--replace-all",
        action="store_true",
        help="replace every occurrence; without this flag, old must match exactly once",
    )
    args = p.parse_args(argv)

    if args.old_stdin and args.new_stdin:
        print("error: only one of --old-stdin / --new-stdin may be set", file=sys.stderr)
        return 5

    target = Path(args.path).resolve()
    if not target.exists():
        print(f"error: {args.path}: no such file", file=sys.stderr)
        return 2

    try:
        old = _read_input("old", args.old_file, args.old_stdin)
        new = _read_input("new", args.new_file, args.new_stdin)
    except SystemExit as e:
        return int(e.code) if isinstance(e.code, int) else 5

    if old == "":
        print("error: old string is empty", file=sys.stderr)
        return 5

    original = target.read_text()
    count = original.count(old)
    if count == 0:
        print(f"error: old string not found in {args.path}", file=sys.stderr)
        return 3
    if count > 1 and not args.replace_all:
        print(
            f"error: old string matches {count} times in {args.path}; use --replace-all",
            file=sys.stderr,
        )
        return 4

    updated = original.replace(old, new) if args.replace_all else original.replace(old, new, 1)

    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as tf:
            tf.write(updated)
            tf.flush()
            os.fsync(tf.fileno())
            tmp_path = tf.name
        os.replace(tmp_path, target)
        tmp_path = None
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)

    diff = difflib.unified_diff(
        original.splitlines(keepends=True),
        updated.splitlines(keepends=True),
        fromfile=str(target),
        tofile=str(target),
    )
    sys.stdout.writelines(diff)
    return 0


if __name__ == "__main__":
    sys.exit(main())
