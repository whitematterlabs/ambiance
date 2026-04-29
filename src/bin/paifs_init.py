#!/usr/bin/env python
"""paifs_init — lay out the v3 FHS skeleton at $PAI_ROOT.

Idempotent. Creates the quasi-Linux directory tree described in
src/usr/share/doc/FILESYSTEM_v3.md, symlinks repo-owned source slots into the
FHS (so dev edits stay live), seeds etc/config.yaml from src/seed/ on
first run, exposes each driver's events.yaml under etc/drivers/, and
provisions a self-contained Python venv at usr/lib/venv/ with runtime
deps + console-script shims at usr/bin/ — so the FHS root is runnable
without reaching back into the repo's own .venv.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tomllib
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"

# Directories to create under $PAI_ROOT.
SKELETON: tuple[str, ...] = (
    "bin",
    "sbin",
    "etc/drivers",
    "etc/prompts",
    "home",
    "root",
    "mnt",
    "opt",
    "proc",
    "run/pai/events",
    "sys/drivers",
    "tmp",
    "usr/lib/drivers",
    "usr/lib/skills",
    "usr/lib/pais",
    "usr/share/prompts",
    "usr/src",
    "var/lib/memory/people",
    "var/lib/memory/topics",
    "var/lib/memory/journal",
    "var/lib/instances",
    "var/lib/packages",
    "var/log/kernel",
    "var/log/drivers",
    "var/log/pai",
    "var/spool/communication/messages",
    "var/cache",
)

# (link_path_under_root, target_in_repo). Symlinks point at the live
# repo so edits are immediately visible. Note: usr/lib/venv and usr/bin
# are NOT symlinks — we provision a real venv + a real bin dir so the
# FHS root is portable and not tethered to the repo's dev .venv.
SYMLINKS: tuple[tuple[str, Path], ...] = (
    ("boot", REPO_ROOT / "src" / "boot"),
    ("usr/src", REPO_ROOT / "src"),
    ("usr/lib/drivers", REPO_ROOT / "src" / "drivers"),
    ("usr/share/prompts", REPO_ROOT / "src" / "prompts"),
    ("usr/share/doc", REPO_ROOT / "src" / "usr" / "share" / "doc"),
)

# Default etc/config.yaml written on first install. Never overwritten —
# once seeded this file is runtime state owned by the agent/user.
DEFAULT_CONFIG_YAML = """\
# PAI kernel control plane.
#
# Source of truth for which long-running PAIs exist. The kernel reconciles
# home/proc/ against this file at boot and on a `kernel:reload_config` event.
# In git, agent-editable.
#
# Field rules (see src/kernel/config.py for the authoritative schema):
#   name         (required) stable proc-dir slug; unique
#   pid          required for reserved entries (1 and 2); auto-allocated otherwise
#   description  required
#   package      (optional) pulls defaults from packages/{package}/package.yaml
#   prompt       per-PAI role file (resolved relative to repo root)
#   model        accepted, persisted into spec.yaml — INERT in v1
#   wake_on      list of fnmatch globs over event-kind; matching PAIs are nudged
#   fallback     if true, this PAI is nudged only when no wake_on pattern matched

pais:
  - name: kernel_manager
    pid: 1
    description: kernel-internal events + errored nudges
    prompt: src/prompts/kernel_manager.md
    model: deepseek-v4-pro
    wake_on: ['kernel:*']

  - name: pai
    pid: 2
    description: owner-facing PAI; catch-all for unclaimed events
    prompt: src/prompts/pai_default.md
    model: deepseek-v4-pro
    fallback: true

  # Example future entry (not seeded):
  # - name: msg-spec
  #   package: message_specialist
  #   wake_on: ['imessage:*']
"""

# These appear in SKELETON but get replaced by symlinks above. The
# symlink wins; ensure_symlink will remove an existing empty dir.
SYMLINK_TARGETS = {p for p, _ in SYMLINKS}

# Scripts that get installed into /sbin/ instead of /usr/bin/. These are
# privileged kernel/owner ops, not PAI-callable tools.
SBIN_SCRIPTS: frozenset[str] = frozenset({"init", "migrate", "reset", "tui"})


def ensure_dir(path: Path) -> None:
    if path.is_symlink() or path.exists():
        return
    path.mkdir(parents=True, exist_ok=True)


def ensure_symlink(link: Path, target: Path) -> None:
    # In-tree case: PAI_ROOT == REPO_ROOT means the link IS the target.
    # Nothing to wire — the FHS slot already holds the canonical content.
    try:
        if link.resolve() == target.resolve():
            return
    except FileNotFoundError:
        pass
    if link.is_symlink():
        if link.readlink() == target:
            return
        link.unlink()
    elif link.exists():
        if link.is_dir() and not any(link.iterdir()):
            link.rmdir()
        else:
            # Already populated at the canonical FHS slot; leave it.
            return
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(target)


def ensure_default_config(root: Path) -> None:
    """Write a default etc/config.yaml on first install. Never overwrites."""
    dest = root / "etc" / "config.yaml"
    if dest.exists():
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(DEFAULT_CONFIG_YAML)


def expose_driver_events(root: Path) -> None:
    """For each driver shipping an events.yaml next to its source code,
    expose it at /etc/drivers/<name>/events.yaml as the FHS spec requires."""
    drivers_src = REPO_ROOT / "src" / "drivers"
    if not drivers_src.is_dir():
        return
    for events_file in drivers_src.glob("*/events.yaml"):
        driver_name = events_file.parent.name
        link = root / "etc" / "drivers" / driver_name / "events.yaml"
        ensure_symlink(link, events_file)


def _load_pyproject() -> dict:
    with PYPROJECT.open("rb") as f:
        return tomllib.load(f)


def ensure_venv(root: Path) -> Path:
    """Create a real venv at usr/lib/venv/ with runtime deps installed.

    Idempotent: skips creation if the venv's python already exists, and
    `uv pip install` itself no-ops when deps are satisfied. We replace
    any pre-existing symlink (legacy: pointed at repo `.venv`)."""
    venv_dir = root / "usr" / "lib" / "venv"
    if venv_dir.is_symlink():
        venv_dir.unlink()
    py = venv_dir / "bin" / "python"
    if not py.exists():
        venv_dir.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["uv", "venv", str(venv_dir)], check=True)
    deps = _load_pyproject().get("project", {}).get("dependencies", [])
    if deps:
        subprocess.run(
            ["uv", "pip", "install", "--python", str(py), *deps],
            check=True,
        )
    return venv_dir


def install_pth(venv_dir: Path, root: Path) -> None:
    """Drop a .pth file in the venv's site-packages so /usr/src/ is on
    sys.path. This is what makes `import kernel` / `import bin.foo`
    work without an editable install."""
    py = venv_dir / "bin" / "python"
    out = subprocess.run(
        [str(py), "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
        check=True,
        capture_output=True,
        text=True,
    )
    site = Path(out.stdout.strip())
    pth = site / "_pai_src.pth"
    pth.write_text(f"{root / 'usr' / 'src'}\n")


def install_bin_shims(venv_dir: Path, root: Path) -> None:
    """Generate shim files for each [project.scripts] entry.

    Splits by privilege: SBIN_SCRIPTS go to sbin/, the rest to usr/bin/.
    Each shim shebangs the FHS venv's python and import-calls the target.
    Idempotent — overwritten on every run so the bin set tracks pyproject."""
    bin_dir = root / "usr" / "bin"
    sbin_dir = root / "sbin"
    for d in (bin_dir, sbin_dir):
        if d.is_symlink():
            d.unlink()
        d.mkdir(parents=True, exist_ok=True)
    py = venv_dir / "bin" / "python"
    scripts = _load_pyproject().get("project", {}).get("scripts", {})
    for name, target in scripts.items():
        module, _, attr = target.partition(":")
        dest_dir = sbin_dir if name in SBIN_SCRIPTS else bin_dir
        shim = dest_dir / name
        shim.write_text(
            f"#!{py}\n"
            f"from {module} import {attr}\n"
            f"raise SystemExit({attr}())\n"
        )
        shim.chmod(0o755)
    # Expose the venv's python at usr/bin/python. Must be an exec shim,
    # not a symlink: CPython resolves argv[0] through symlinks, so a
    # symlink chain landing back at the uv-managed binary loses the
    # venv (no adjacent pyvenv.cfg). The exec shim preserves argv[0].
    py_shim = bin_dir / "python"
    if py_shim.is_symlink() or py_shim.exists():
        py_shim.unlink()
    py_shim.write_text(f'#!/bin/sh\nexec "{py}" "$@"\n')
    py_shim.chmod(0o755)


def lay_out(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for rel in SKELETON:
        if rel in SYMLINK_TARGETS:
            continue
        ensure_dir(root / rel)
    for rel, target in SYMLINKS:
        ensure_symlink(root / rel, target)
    ensure_default_config(root)
    expose_driver_events(root)
    venv_dir = ensure_venv(root)
    install_pth(venv_dir, root)
    install_bin_shims(venv_dir, root)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--root",
        type=Path,
        default=Path(os.environ.get("PAI_ROOT", str(Path.home() / ".pai"))),
        help="FHS root (default: $PAI_ROOT or ~/.pai)",
    )
    args = ap.parse_args()
    lay_out(args.root)
    print(f"FHS skeleton ready at {args.root}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
