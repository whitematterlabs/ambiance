"""
Reset live/ to an empty scaffold matching SCAFFOLDING.md.

Deletes all data under live/ and recreates the empty directory structure.
"""

import shutil
from pathlib import Path

LIVE_DIR = Path(__file__).resolve().parent.parent / "live"

SCAFFOLD_DIRS = [
    "communication/messages",
    "memory/myself",
    "memory/people",
    "memory/topics",
    "memory/journal",
    "memory/skills",
    "proc",
    "events",
    "tmp",
    "workspace",
]


def main():
    if not LIVE_DIR.exists():
        print("live/ does not exist, creating fresh scaffold.")
    else:
        # Remove everything except PAI.md
        for child in LIVE_DIR.iterdir():
            if child.name == "PAI.md":
                continue
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
        print("Cleared live/.")

    for d in SCAFFOLD_DIRS:
        (LIVE_DIR / d).mkdir(parents=True, exist_ok=True)

    print("Scaffold restored.")


if __name__ == "__main__":
    main()
