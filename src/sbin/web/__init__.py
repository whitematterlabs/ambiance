"""PAI web surface — an owner console that attaches to a running kernel.

Like the TUI, this surface only reads the on-disk FHS state and performs the
same two writes (a me-thread day-file line + an event file). It never owns or
drives the kernel: TUI/GUI/other owner surface <-> kernel <-> LLM.
"""
