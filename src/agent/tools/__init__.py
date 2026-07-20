"""The agent's tool suite.

Each module exports TOOL_NAME, TOOL_SCHEMA, and run() (async for the
shell-backed tools, sync for the file tools); results render via
`.render()` (shell) or `.text`/`.is_error` (file). `agent.llm` dispatches
to them by TOOL_NAME.
"""

from . import bash, edit, noop, read, shell, write

__all__ = ["bash", "edit", "noop", "read", "shell", "write"]
