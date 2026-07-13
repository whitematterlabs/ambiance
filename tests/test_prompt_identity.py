"""The <pai-instance> identity line, with and without an owner-chosen name."""

from __future__ import annotations

from boot import bootstrap


def test_pai_line_default_is_pid_identity():
    line = bootstrap._pai_line(4, None)
    assert line.startswith("You are PAI pid 4. Parent: kernel.")


def test_pai_line_renders_display_name():
    line = bootstrap._pai_line(4, None, "Muse")
    assert line.startswith("You are Muse, PAI pid 4. Parent: kernel.")


def test_pai_line_blank_display_name_falls_back():
    assert bootstrap._pai_line(4, 2, "   ") == bootstrap._pai_line(4, 2)


def test_system_prompt_carries_display_name(tmp_path):
    prompt = bootstrap.build_system_prompt(
        pai=4, home_dir=str(tmp_path), display_name="Muse"
    )
    assert "You are Muse, PAI pid 4" in prompt
