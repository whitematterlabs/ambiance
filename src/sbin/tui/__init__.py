"""Shared parsing/watcher helpers (formerly the owner-facing TUI).

The TUI itself is deprecated (see `deprecated/tui/`); the web console is now the
sole owner surface. Only `state.py` remains here — the web surface imports it
(`sbin.tui.state`) so the on-disk message format has one parser.
"""
