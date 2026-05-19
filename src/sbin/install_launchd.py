"""pai-install-launchd — install/uninstall the kernel LaunchAgent.

Reads the plist template from the live repo (via $PAI_ROOT/usr/src/, which
paifs-init symlinks to the repo), substitutes YOUR_HOME, writes it to
~/Library/LaunchAgents/com.pai.kernel.plist, then bootstraps + enables it
with launchctl. Idempotent: bootouts an existing job before re-bootstrapping.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from boot.paths import PAI_ROOT

LABEL = "com.pai.kernel"
PLIST_NAME = f"{LABEL}.plist"


def _agents_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def _installed_plist() -> Path:
    return _agents_dir() / PLIST_NAME


def _template_plist() -> Path:
    # paifs-init symlinks $PAI_ROOT/usr/src → <repo>/src/. The plist lives at
    # <repo>/macos/launchd/, one level above the symlink target.
    repo_root = (PAI_ROOT / "usr" / "src").resolve().parent
    return repo_root / "macos" / "launchd" / PLIST_NAME


def _uid() -> int:
    return os.getuid()


def _domain() -> str:
    return f"gui/{_uid()}"


def _service() -> str:
    return f"{_domain()}/{LABEL}"


def _is_bootstrapped() -> bool:
    r = subprocess.run(
        ["launchctl", "print", _service()],
        capture_output=True,
        text=True,
    )
    return r.returncode == 0


def cmd_install() -> int:
    src = _template_plist()
    if not src.exists():
        print(f"pai-install-launchd: template not found at {src}", file=sys.stderr)
        return 1
    home = str(Path.home())
    content = src.read_text().replace("YOUR_HOME", home)
    dest = _installed_plist()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(content)
    print(f"wrote {dest}")

    if _is_bootstrapped():
        print(f"bootout existing {_service()}")
        subprocess.run(["launchctl", "bootout", _service()], check=False)

    subprocess.run(["launchctl", "bootstrap", _domain(), str(dest)], check=True)
    subprocess.run(["launchctl", "enable", _service()], check=True)
    subprocess.run(["launchctl", "kickstart", "-k", _service()], check=False)
    print(f"bootstrapped {_service()}")
    return 0


def cmd_uninstall() -> int:
    if _is_bootstrapped():
        subprocess.run(["launchctl", "bootout", _service()], check=False)
        print(f"bootout {_service()}")
    dest = _installed_plist()
    if dest.exists():
        dest.unlink()
        print(f"removed {dest}")
    else:
        print(f"no plist at {dest}")
    return 0


def cmd_status() -> int:
    if not _is_bootstrapped():
        print(f"{_service()} not installed")
        return 1
    r = subprocess.run(["launchctl", "print", _service()])
    return r.returncode


def main() -> int:
    if sys.platform != "darwin":
        print("pai-install-launchd: macOS only", file=sys.stderr)
        return 1
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "action",
        nargs="?",
        default="install",
        choices=("install", "uninstall", "status"),
    )
    args = ap.parse_args()
    return {
        "install": cmd_install,
        "uninstall": cmd_uninstall,
        "status": cmd_status,
    }[args.action]()


if __name__ == "__main__":
    sys.exit(main())
