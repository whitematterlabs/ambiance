"""Entry point: `uv run python -m tui`."""

from __future__ import annotations

from .app import TuiApp


def main() -> None:
    TuiApp().run()


if __name__ == "__main__":
    main()
