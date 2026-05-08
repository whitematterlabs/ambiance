#!/usr/bin/env python
"""paifs_init — lay out the v3 FHS skeleton at $PAI_ROOT.

Idempotent. Creates the quasi-Linux directory tree described in
src/usr/share/doc/FILESYSTEM_v3.md, symlinks repo-owned source slots into the
FHS (so dev edits stay live), seeds etc/config.yaml from src/seed/ on
first run, and provisions a self-contained Python venv at
usr/lib/venv/ with runtime
deps + console-script shims at usr/bin/ — so the FHS root is runnable
without reaching back into the repo's own .venv.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path


def _ensure_uv() -> None:
    """Hard-fail with install instructions if `uv` is missing.

    The whole FHS provisioning chain (venv creation, pip installs, paiman
    deps) goes through uv. We don't auto-install it — curl-pipe-sh feels
    inappropriate for a tool that touches Python toolchain — but we do
    surface a single clear message instead of an opaque FileNotFoundError
    deep in subprocess.run.
    """
    if shutil.which("uv") is not None:
        return
    sys.exit(
        "paifs-init: `uv` is required but not on PATH.\n"
        "Install it first:\n"
        "    brew install uv                                   # macOS\n"
        "    curl -LsSf https://astral.sh/uv/install.sh | sh   # any unix\n"
        "Then re-run paifs-init."
    )


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"

# Directories to create under $PAI_ROOT.
SKELETON: tuple[str, ...] = (
    "sbin",
    "dev",
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
    "usr/lib/subagents",
    "usr/libexec",
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
    ("usr/share/doc", REPO_ROOT / "src" / "usr" / "share" / "doc"),
)

# Prompts paifs-init seeds via paiman so the kernel boots on first run.
# Scope: only what the seed config.yaml references. App PAIs/drivers/skills
# get installed later by the root user via `paiman install <name>`.
ROOT_SEED_PROMPTS: tuple[str, ...] = (
    "root",
    "pai_default",
    # Sysprompt fragment stitched in by build_system_prompt for every
    # non-root, non-subagent PAI. Not a role itself — shared across roles.
    "capability-escalation",
)

# Drivers the kernel imports as libraries at module-load time. A fresh
# $PAI_ROOT must have these or `import drivers.contacts` / `drivers.messages`
# raises during boot. Drivers with runnable processes (imessage, macmail)
# are NOT seeded — the root user installs them explicitly.
KERNEL_SEED_DRIVERS: tuple[str, ...] = ("contacts", "messages")

# Skills every PAI needs at first boot. Kept tight: only skills that
# teach the use of a kernel-provided tool the PAI cannot reasonably
# invent on its own.
KERNEL_SEED_SKILLS: tuple[str, ...] = (
    "schedule-reminder",
    "grow-capability",
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
# Field rules (see src/boot/config.py for the authoritative schema):
#   name         (required) stable proc-dir slug; unique
#   pid          required for reserved entries (1 and 2); auto-allocated otherwise
#   description  required
#   package      (optional) pulls defaults from packages/{package}/package.yaml
#   prompt       per-PAI role file (resolved relative to repo root)
#   provider     LLM provider key (anthropic | deepseek). Drives base_url + key.
#   model        model id within the provider; defaults to provider's default
#   wake_on      list of fnmatch globs over event-kind; matching PAIs are nudged
#   fallback     if true, this PAI is nudged only when no wake_on pattern matched

pais:
  - name: root
    pid: 1
    description: kernel-internal events + errored nudges
    prompt: src/prompts/root.md
    provider: deepseek
    model: deepseek-v4-pro
    wake_on: ['kernel:*']

  - name: pai
    pid: 2
    description: owner-facing PAI; catch-all for unclaimed events
    prompt: src/prompts/pai_default.md
    provider: deepseek
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
SBIN_SCRIPTS: frozenset[str] = frozenset({
    "init",
    "migrate",
    "reboot",
    "reset",
    "tui",
    "paiman",
    "paiadd",
    "paidel",
    "paifs-init",
})


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
    pth.write_text(
        f"{root / 'usr' / 'lib'}\n"
        f"{root / 'usr' / 'src'}\n"
    )


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
        # Remove any stale shim in the *other* dir so privilege moves
        # (bin → sbin or back) don't leave duplicates.
        stale = (bin_dir if dest_dir is sbin_dir else sbin_dir) / name
        if stale.exists() or stale.is_symlink():
            stale.unlink()
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
    # /bin → usr/bin (relative). One bin for PAI-callable tools; /sbin
    # holds the kernel-only ones.
    ensure_symlink(root / "bin", Path("usr/bin"))
    ensure_default_config(root)
    ensure_system_deps()
    venv_dir = ensure_venv(root)
    install_pth(venv_dir, root)
    install_bin_shims(venv_dir, root)
    seed_kernel_essentials(root)


# System-level binaries the kernel itself shells out to. Drivers may add
# their own via libexec/install.sh — this is the floor.
SYSTEM_DEPS: tuple[str, ...] = (
    "tmux",   # shell_tool drives PAI bash sessions through tmux
)


def ensure_system_deps() -> None:
    """Install kernel-required system binaries via Homebrew (macOS only).

    Idempotent: brew install no-ops when the package is already present.
    """
    import shutil
    if sys.platform != "darwin":
        return
    brew = shutil.which("brew")
    if not brew:
        print("warning: brew not found; cannot install system deps "
              f"({', '.join(SYSTEM_DEPS)}). Install Homebrew or these manually.")
        return
    for pkg in SYSTEM_DEPS:
        if shutil.which(pkg):
            continue
        print(f"installing system dep: {pkg}")
        subprocess.run([brew, "install", pkg], check=True)


def seed_kernel_essentials(root: Path) -> None:
    """Install the prompts and drivers the kernel needs to boot.

    Prompts: whatever the seed config.yaml references (root, pai_default).
    Drivers: contacts + messages, imported at module-load by the kernel.

    Idempotent: skips items already installed. Uses paiman's default registry."""
    paiman = root / "sbin" / "paiman"
    if not paiman.exists():
        print(f"warning: {paiman} not found; skipping kernel essentials seed")
        return
    env = {**os.environ, "PAI_ROOT": str(root)}

    prompts_dir = root / "usr" / "share" / "prompts"
    needed_prompts = [
        name for name in ROOT_SEED_PROMPTS
        if not (prompts_dir / f"{name}.md").is_symlink()
    ]
    drivers_dir = root / "usr" / "lib" / "drivers"
    needed_drivers = [
        name for name in KERNEL_SEED_DRIVERS
        if not (drivers_dir / name / "events.yaml").exists()
        and not (drivers_dir / name / "package.yaml").exists()
    ]
    skills_dir = root / "usr" / "lib" / "skills"

    def _skill_installed(name: str) -> bool:
        if (skills_dir / name / "SKILL.md").exists():
            return True
        if not skills_dir.is_dir():
            return False
        for topic_dir in skills_dir.iterdir():
            if not topic_dir.is_dir():
                continue
            if (topic_dir / name / "SKILL.md").exists():
                return True
        return False

    needed_skills = [
        name for name in KERNEL_SEED_SKILLS if not _skill_installed(name)
    ]
    for name in needed_prompts + needed_drivers + needed_skills:
        subprocess.run([str(paiman), "install", name], check=True, env=env)


def expose_pai_command(root: Path) -> None:
    """Symlink the `pai` entrypoint into the first writable system bin dir.

    Tries /usr/local/bin → /opt/homebrew/bin → ~/.local/bin (created if absent).
    Only `pai` is exposed; PAI itself doesn't need a launcher once running.
    """
    target = root / "usr" / "bin" / "pai"
    candidates = [
        Path("/usr/local/bin"),
        Path("/opt/homebrew/bin"),
        Path.home() / ".local" / "bin",
    ]
    for parent in candidates:
        if parent == Path.home() / ".local" / "bin":
            parent.mkdir(parents=True, exist_ok=True)
        if not parent.exists() or not os.access(parent, os.W_OK):
            continue
        link = parent / "pai"
        if link.is_symlink() and link.readlink() == target:
            print(f"`pai` available at {link}")
            return
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(target)
        print(f"`pai` available at {link}")
        return
    print(f"note: no writable bin dir found; add {target.parent} to PATH manually")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--root",
        type=Path,
        default=Path(os.environ.get("PAI_ROOT", str(Path.home() / ".pai"))),
        help="FHS root (default: $PAI_ROOT or ~/.pai)",
    )
    args = ap.parse_args()
    _ensure_uv()
    lay_out(args.root)
    expose_pai_command(args.root)
    print(f"FHS skeleton ready at {args.root}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
