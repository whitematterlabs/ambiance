"""Deprecated owner-facing TUI.

Superseded by the web console (`pai start`). Kept for reference only; not
packaged or on the import path. The pure parsing/watcher helpers this UI used
still live at `src/sbin/tui/state.py`, which the web surface imports.
"""

from .__main__ import main  # noqa: F401
