#!/usr/bin/env python
"""paifs_init — lay out the v3 FHS skeleton at $PAI_ROOT.

Idempotent. Creates the quasi-Linux directory tree described in
src/usr/share/doc/FILESYSTEM_v3.md, symlinks repo-owned source slots into the
FHS (so dev edits stay live), seeds etc/config.yaml from src/seed/ on
first run, and provisions a self-contained Python venv at
usr/lib/venv/ with runtime
deps + console-script shims at usr/bin/ — so the FHS root is runnable
without reaching back into the repo's own .venv.

Two modes:
  - **dev** (default): symlinks `REPO_ROOT/src/*` into the FHS and builds a
    `uv` venv. Edits in the repo are live; assumes a checkout + `uv`.
  - **bundle** (`--bundle-mode --seed <dir>`): used by PAI.app on first run.
    There is no checkout and no `uv` — the embedded interpreter already has the
    package, and content slots are *copied* from the bundled `<seed>` dir rather
    than symlinked at a repo. Writes a `var/lib/.provisioned` schema marker so
    the app can cheaply detect first-run and re-provision on a version bump.
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
    "etc/boilerplate",
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
    ("usr/libexec/web", REPO_ROOT / "src" / "usr" / "libexec" / "web"),
    ("usr/share/doc", REPO_ROOT / "src" / "usr" / "share" / "doc"),
    # owner.md is intentionally NOT here: it holds per-owner PII and is
    # gitignored ("not shipped"), so a fresh clone has no source to point at.
    # It is generated in the FHS by ensure_default_owner() and the boilerplate
    # slot is a relative link to it (both wired in lay_out).
    ("etc/boilerplate/memory-usage.md", REPO_ROOT / "src" / "etc" / "boilerplate" / "memory-usage.md"),
    ("etc/boilerplate/capability-escalation.md", REPO_ROOT / "src" / "etc" / "boilerplate" / "capability-escalation.md"),
)

# Prompts paifs-init seeds via paiman so the kernel boots on first run.
# Scope: only what the seed config.yaml references. App PAIs/drivers/skills
# get installed later by the root user via `paiman install <name>`.
ROOT_SEED_PROMPTS: tuple[str, ...] = (
    "root",
    "pai_default",
    # Sysprompt fragments stitched in for spawned subagents so the child
    # knows it IS the subagent and shouldn't recursively spawn another.
    "subagent",
    "subagent-persistent",
)

# Drivers the kernel imports as libraries during boot/runtime. A fresh
# $PAI_ROOT must have these before `boot.entry` starts supervising; drivers
# with runnable processes (imessage, macmail)
# are NOT seeded — the root user installs them explicitly.
KERNEL_SEED_DRIVERS: tuple[str, ...] = ("contacts", "messages")

# Skills every PAI needs at first boot. Kept tight: only skills that
# teach the use of a kernel-provided tool the PAI cannot reasonably
# invent on its own.
KERNEL_SEED_SKILLS: tuple[str, ...] = (
    "schedule-reminder",
    "grow-capability",
)

# Bins the kernel's memory contract refers to from the default prompts.
# `memorize` and `remember` are invoked by every PAI via the memory-usage
# boilerplate; without them installed the memory contract is inert.
KERNEL_SEED_BINS: tuple[str, ...] = (
    "memorize",
    "remember",
)

# PAIs the kernel itself requires to close core loops. `librarian-pai`
# is the sole writer to shared/private MEMORY indexes and the consumer
# of `memorize`/`remember` requests; the default config below declares it as a
# reserved fleet member so reconcile spawns it on first boot.
KERNEL_SEED_PAIS: tuple[str, ...] = (
    "librarian-pai",
)

# Provider/model the seed config.yaml uses when install.sh doesn't pass an
# owner choice (non-interactive installs, re-runs). Mirrors llm.PROVIDERS.
DEFAULT_SEED_PROVIDER = "deepseek"
DEFAULT_SEED_MODEL = "deepseek-v4-pro"


def default_config_yaml(provider: str = DEFAULT_SEED_PROVIDER,
                        model: str = DEFAULT_SEED_MODEL) -> str:
    """Render the etc/config.yaml seeded on first install.

    `provider`/`model` default to deepseek (preserving prior behavior) but are
    overridden by install.sh's interactive prompt so the whole fleet boots on
    the model the owner picked — and so only that provider's API key is needed.
    Written once and never overwritten; afterward it's owner-editable state.
    """
    return f"""\
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
#   package      (optional) pulls defaults from packages/{{package}}/package.yaml
#   prompt       per-PAI role file (resolved relative to repo root)
#   provider     LLM provider key (anthropic | deepseek). Drives base_url + key.
#   model        model id within the provider; defaults to provider's default
#   wake_on      list of fnmatch globs over event-kind; matching PAIs are nudged
#   fallback     if true, this PAI is nudged only when no wake_on pattern matched

# First-wake owner profiling. While true, the fallback PAI's first nudge gets a
# one-time instruction to skim the owner's last month of mail/messages/contacts/
# calendar and write var/lib/owner/profile.md. The kernel clears this to false
# once that file exists (idempotent retry until it does).
onboarding_pending: true

pais:
  - name: root
    pid: 1
    description: kernel-internal events + errored nudges
    prompt_dir: opt/paiman/root
    boilerplate: [owner]
    provider: {provider}
    model: {model}
    wake_on: ['kernel:*']

  - name: pai
    pid: 2
    description: owner-facing PAI; catch-all for unclaimed events
    prompt_dir: opt/paiman/pai_default
    boilerplate: [owner, memory-usage, capability-escalation]
    provider: {provider}
    model: {model}
    fallback: true

  - name: librarian-pai
    package: librarian-pai
    description: nightly + on-demand memory consolidator; sole writer of shared/private MEMORY

  # Example future entry (not seeded):
  # - name: msg-spec
  #   package: message_specialist
  #   wake_on: ['imessage:*']
"""

# These appear in SKELETON but get replaced by symlinks above. The
# symlink wins; ensure_symlink will remove an existing empty dir.
SYMLINK_TARGETS = {p for p, _ in SYMLINKS}

# Bundle-mode provisioning schema. Bumped when the on-disk layout this script
# produces changes in a way that warrants re-provisioning an existing root.
# Written to var/lib/.provisioned; PAI.app re-provisions when its current
# schema exceeds the marker's.
PROVISION_SCHEMA = 1

# Content slots bundle mode COPIES (not symlinks) out of the bundled seed dir.
# Each entry is (link_path_under_root, seed_relative_path). A bundled build is
# expected to ship `src/etc/` and `src/usr/share/doc/` under its seed dir as
# `etc/` and `doc/`; these mappings mirror the content entries in SYMLINKS
# above, sourced from the seed instead of the repo.
BUNDLE_SEED_CONTENT: tuple[tuple[str, str], ...] = (
    ("usr/share/doc", "doc"),
    # owner.md is generated (ensure_default_owner), not copied from the seed —
    # it carries per-owner PII and is never shipped in a bundle.
    ("etc/boilerplate/memory-usage.md", "etc/boilerplate/memory-usage.md"),
    ("etc/boilerplate/capability-escalation.md", "etc/boilerplate/capability-escalation.md"),
)

# Scripts that get installed into /sbin/ instead of /usr/bin/. These are
# privileged kernel/owner ops, not PAI-callable tools.
SBIN_SCRIPTS: frozenset[str] = frozenset({
    "init",
    "pai",
    "migrate",
    "reboot",
    "reset",
    "tui",
    "paiman",
    "paiadd",
    "paiclone",
    "paidel",
    "paifs-init",
    "paisetup",
})


def ensure_dir(path: Path) -> None:
    if path.is_symlink() or path.exists():
        return
    path.mkdir(parents=True, exist_ok=True)


def ensure_symlink(link: Path, target: Path) -> None:
    # Resolve a relative target (only `bin -> usr/bin`) the way the symlink
    # itself will — relative to the link's own directory. Everything else is
    # an absolute repo path.
    src = target if target.is_absolute() else (link.parent / target)
    # Only the absolute repo-source links are checked for existence. The one
    # relative target (`bin -> usr/bin`) is a deliberate forward ref — usr/bin
    # is provisioned later by install_bin_shims — so it legitimately points at
    # a not-yet-existing slot.
    if target.is_absolute() and not src.exists():
        # The source this slot must point at is gone: an incomplete checkout,
        # or paifs-init running from the wrong tree (e.g. a ~/.pai copied off
        # another machine, whose links reference a repo path that doesn't
        # exist here). Fail loud and specific *now* instead of letting the
        # kernel abort three boot phases later with an opaque config error
        # like "boilerplate `owner` not found". We exit before touching the
        # existing link, so any dangling link is left in place and the next
        # run self-heals once the source is restored.
        sys.exit(
            f"paifs-init: symlink source missing: {src}\n"
            f"  needed by: {link}\n"
            f"  fix: re-run paifs-init from this machine's complete pai checkout."
        )
    # In-tree case: PAI_ROOT == REPO_ROOT means the link IS the target.
    # Nothing to wire — the FHS slot already holds the canonical content.
    try:
        if link.resolve() == src.resolve():
            return
    except OSError:
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


def ensure_default_config(root: Path, *, provider: str = DEFAULT_SEED_PROVIDER,
                          model: str = DEFAULT_SEED_MODEL) -> None:
    """Write a default etc/config.yaml on first install. Never overwrites."""
    dest = root / "etc" / "config.yaml"
    if dest.exists():
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(default_config_yaml(provider, model))


DEFAULT_OWNER_MD = """\
Owner metadata. Use these when filling forms, addressing mail, scheduling
deliveries, drafting replies that need contact details, etc. Don't volunteer
them unprompted.

Fill in the owner's real details below. This file is per-owner and is never
committed (it's gitignored as "not shipped"); it lives in the FHS, not the
repo. After editing, the values are picked up via the `owner` boilerplate.

- Name:
- Address:
- Phone:
- Email:
"""


def ensure_default_owner(root: Path) -> None:
    """Write a placeholder etc/owner.md on first install. Never overwrites.

    owner.md carries per-owner PII and is deliberately kept out of git, so a
    fresh clone or bundle ships no source for it. Mirroring ensure_default_config,
    we generate a template here so the `owner` boilerplate source always exists
    and the kernel boots; the owner fills in real values afterward. A leftover
    *dangling* symlink (from the pre-generate layout, where owner.md pointed at
    an absent repo file) is replaced so the broken state self-heals."""
    dest = root / "etc" / "owner.md"
    if dest.exists():
        # Real file or a live symlink to the owner's data — keep it untouched.
        return
    if dest.is_symlink():
        # Dangling link left by an older layout; drop it and seed the template.
        dest.unlink()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(DEFAULT_OWNER_MD)


SHARED_MEMORY_INDEX_HEADER = (
    "<!-- Fleet-wide MEMORY index. Owned by the librarian PAI; fleet PAIs read but do not edit. -->\n"
)


def ensure_shared_memory_index(root: Path) -> None:
    """Seed var/lib/memory/MEMORY.md so the boilerplate's claim of an index is true on disk."""
    dest = root / "var" / "lib" / "memory" / "MEMORY.md"
    if dest.exists():
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(SHARED_MEMORY_INDEX_HEADER)


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


def _console_scripts(venv_dir: Path | None) -> dict[str, str]:
    """Map console-script name -> 'module:attr'.

    Dev mode (venv_dir set) reads pyproject.toml — the repo is on disk. Bundle
    mode (venv_dir None) has no pyproject in the app, so it reads the installed
    `pai` dist's entry points from the running interpreter's metadata."""
    if venv_dir is not None:
        return _load_pyproject().get("project", {}).get("scripts", {})
    from importlib.metadata import entry_points

    out: dict[str, str] = {}
    for ep in entry_points(group="console_scripts"):
        dist = getattr(ep, "dist", None)
        name = getattr(dist, "name", None)
        if name and name.replace("_", "-").lower() == "pai":
            out[ep.name] = ep.value
    return out


def install_bin_shims(venv_dir: Path | None, root: Path, *, python: Path | None = None) -> None:
    """Generate shim files for each console-script entry.

    Splits by privilege: SBIN_SCRIPTS go to sbin/, the rest to usr/bin/.
    Each shim shebangs `python` (the FHS venv python in dev mode, the embedded
    interpreter in bundle mode) and import-calls the target. Idempotent —
    overwritten on every run so the bin set tracks the package's entry points.

    Dev mode passes `venv_dir` (scripts read from pyproject, python is the
    venv's). Bundle mode passes `venv_dir=None` + an explicit `python` (scripts
    read from installed metadata)."""
    bin_dir = root / "usr" / "bin"
    sbin_dir = root / "sbin"
    for d in (bin_dir, sbin_dir):
        if d.is_symlink():
            d.unlink()
        d.mkdir(parents=True, exist_ok=True)
    py = python if python is not None else venv_dir / "bin" / "python"
    scripts = _console_scripts(venv_dir)
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


def repoint_shims(root: Path, python_exe: Path) -> int:
    """Rewrite every tool-shim shebang under usr/bin + sbin to `python_exe`
    (and the usr/bin/python exec-shim's target). Returns the count changed.

    Shims are generated at provision time pointing at whatever python ran
    paifs-init — the dev venv. A standalone PAI.app must NOT depend on that
    venv, so on launch it re-points the shims at its embedded interpreter.
    Deliberately bundle-safe: touches only existing shim files, so it needs
    no uv, no venv, and no pyproject.toml (none of which a shipped app has).
    Idempotent — skips shims already pointing at `python_exe`."""
    py = str(python_exe)
    changed = 0
    for d in (root / "usr" / "bin", root / "sbin"):
        if not d.is_dir():
            continue
        for shim in d.iterdir():
            if shim.is_symlink() or not shim.is_file():
                continue
            try:
                text = shim.read_text()
            except (OSError, UnicodeDecodeError):
                continue
            lines = text.split("\n")
            if not lines or not lines[0].startswith("#!"):
                continue
            if shim.name == "python" and lines[0] == "#!/bin/sh":
                # The exec-shim that exposes the interpreter at usr/bin/python.
                new = f'#!/bin/sh\nexec "{py}" "$@"\n'
            else:
                # A console-script shim: `#!<py>` + import/call body.
                new = f"#!{py}\n" + "\n".join(lines[1:])
            if new == text:
                continue
            shim.write_text(new)
            shim.chmod(0o755)
            changed += 1
    return changed


def copy_seed_content(root: Path, seed: Path) -> None:
    """Copy the seed content slots into the FHS (bundle mode).

    Dev mode symlinks these at the live repo (see SYMLINKS); a shipped app has
    no repo, so it copies the equivalents out of the bundled `<seed>` dir.
    Overwrites on every run so the on-disk copy tracks the shipped seed."""
    for link_rel, seed_rel in BUNDLE_SEED_CONTENT:
        src = seed / seed_rel
        if not src.exists():
            sys.exit(f"paifs-init: seed content missing: {src}")
        dest = root / link_rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.is_symlink():
            dest.unlink()
        if src.is_dir():
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(src, dest)
        else:
            if dest.exists():
                dest.unlink()
            shutil.copy2(src, dest)


def write_provisioned_marker(root: Path) -> None:
    """Stamp var/lib/.provisioned with the schema version (bundle mode).

    Lets PAI.app cheaply detect an unprovisioned root (no marker) and
    re-provision when its bundled schema exceeds the marker's."""
    dest = root / "var" / "lib" / ".provisioned"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(f"{PROVISION_SCHEMA}\n")


def lay_out(root: Path, *, bundle_mode: bool = False, seed: Path | None = None,
            default_provider: str = DEFAULT_SEED_PROVIDER,
            default_model: str = DEFAULT_SEED_MODEL) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for rel in SKELETON:
        if rel in SYMLINK_TARGETS:
            continue
        ensure_dir(root / rel)
    if bundle_mode:
        if seed is None:
            sys.exit("paifs-init: --bundle-mode requires --seed <dir>")
        # Code symlinks are skipped (code is in the bundle); content slots are
        # copied from the seed instead of symlinked at a repo checkout.
        copy_seed_content(root, seed)
    else:
        for rel, target in SYMLINKS:
            ensure_symlink(root / rel, target)
    # /bin → usr/bin (relative). One bin for PAI-callable tools; /sbin
    # holds the kernel-only ones.
    ensure_symlink(root / "bin", Path("usr/bin"))
    ensure_default_config(root, provider=default_provider, model=default_model)
    # owner.md is generated (PII, not shipped) and the boilerplate slot is a
    # relative link to it — same in both dev and bundle mode.
    ensure_default_owner(root)
    ensure_symlink(root / "etc" / "boilerplate" / "owner.md", Path("../owner.md"))
    ensure_shared_memory_index(root)
    if bundle_mode:
        # No dev venv: the embedded interpreter already has the package, and the
        # `drivers` namespace resolves via PYTHONPATH=usr/lib (set by
        # KernelLauncher). System binaries come from the bundle (PATH), not brew.
        install_bin_shims(None, root, python=Path(sys.executable))
        seed_kernel_essentials(root)
        write_provisioned_marker(root)
    else:
        ensure_system_deps()
        venv_dir = ensure_venv(root)
        install_pth(venv_dir, root)
        install_bin_shims(venv_dir, root)
        seed_kernel_essentials(root)


# System-level binaries the kernel itself shells out to. Drivers may add
# their own via libexec/install.sh — this is the floor. Each entry is
# (brew_formula, binary_name); they differ for some formulas (e.g.
# corelocationcli ships /opt/homebrew/bin/CoreLocationCLI).
SYSTEM_DEPS: tuple[tuple[str, str], ...] = (
    ("tmux", "tmux"),                          # shell_tool spawns a viewer tmux per PAI
    ("corelocationcli", "CoreLocationCLI"),    # bootstrap.py reads location for the per-turn header
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
        formulas = ", ".join(f for f, _ in SYSTEM_DEPS)
        print("warning: brew not found; cannot install system deps "
              f"({formulas}). Install Homebrew or these manually.")
        return
    for formula, binary in SYSTEM_DEPS:
        if shutil.which(binary):
            continue
        print(f"installing system dep: {formula}")
        subprocess.run([brew, "install", formula], check=True)


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
    bin_dir = root / "usr" / "bin"
    needed_bins = [
        name for name in KERNEL_SEED_BINS
        if not (bin_dir / name).exists() and not (bin_dir / name).is_symlink()
    ]
    pais_dir = root / "usr" / "lib" / "pais"
    needed_pais = [
        name for name in KERNEL_SEED_PAIS
        if not (pais_dir / name / "package.yaml").exists()
    ]
    # Use typed `<kind>/<name>` form so `subagent` resolves to the prompt
    # rather than colliding with `bin/subagent`.
    typed = (
        [f"prompts/{n}" for n in needed_prompts]
        + [f"drivers/{n}" for n in needed_drivers]
        + [f"skills/{n}" for n in needed_skills]
        + [f"bin/{n}" for n in needed_bins]
        + [f"pais/{n}" for n in needed_pais]
    )
    for src in typed:
        subprocess.run([str(paiman), "install", src], check=True, env=env)


def expose_pai_command(root: Path) -> None:
    """Symlink the `pai` entrypoint into the first writable system bin dir.

    Tries /usr/local/bin → /opt/homebrew/bin → ~/.local/bin (created if absent).
    Only `pai` is exposed; PAI itself doesn't need a launcher once running.
    """
    target = root / "sbin" / "pai"
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


def _config_has_only_seeds(root: Path) -> bool:
    """True if etc/config.yaml exists and lists only the reserved seed
    PAIs (root + pai). Used to decide whether to chain into paisetup on
    a fresh install."""
    import yaml as _yaml

    cfg = root / "etc" / "config.yaml"
    if not cfg.exists():
        return False
    try:
        data = _yaml.safe_load(cfg.read_text()) or {}
    except _yaml.YAMLError:
        return False
    pais = data.get("pais") or []
    names = {p.get("name") for p in pais if isinstance(p, dict)}
    return names == {"root", "pai", *KERNEL_SEED_PAIS}


def maybe_chain_paisetup(root: Path) -> None:
    """On an interactive TTY against a fresh config, exec into paisetup
    so the user gets a guided first-run. Non-TTY/scripted runs skip it."""
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return
    if not _config_has_only_seeds(root):
        return
    paisetup = root / "sbin" / "paisetup"
    if not paisetup.exists():
        return
    print(f"\nLaunching paisetup to configure your first PAIs…")
    env = {**os.environ, "PAI_ROOT": str(root)}
    os.execvpe(str(paisetup), [str(paisetup)], env)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--root",
        type=Path,
        default=Path(os.environ.get("PAI_ROOT", str(Path.home() / ".pai"))),
        help="FHS root (default: $PAI_ROOT or ~/.pai)",
    )
    ap.add_argument(
        "--no-setup",
        action="store_true",
        help="skip auto-chaining into paisetup on a fresh install",
    )
    ap.add_argument(
        "--bundle-mode",
        action="store_true",
        help="provision from a shipped app (no repo, no uv): copy seed content "
             "instead of symlinking the repo, shim at the embedded interpreter, "
             "and stamp the .provisioned marker. Requires --seed.",
    )
    ap.add_argument(
        "--seed",
        type=Path,
        default=None,
        help="bundled seed dir for --bundle-mode (Resources/seed; holds etc/ and doc/)",
    )
    ap.add_argument(
        "--repoint-shims",
        action="store_true",
        help="rewrite existing tool-shim shebangs to --python and exit "
             "(no provisioning). PAI.app uses this on launch to bind the "
             "shims to its embedded interpreter, off the dev venv.",
    )
    ap.add_argument(
        "--python",
        type=Path,
        default=None,
        help="interpreter for --repoint-shims (default: the one running this)",
    )
    ap.add_argument(
        "--default-provider",
        default=DEFAULT_SEED_PROVIDER,
        help="provider key the seed config.yaml boots the fleet on "
             f"(default: {DEFAULT_SEED_PROVIDER}). Ignored if config.yaml exists.",
    )
    ap.add_argument(
        "--default-model",
        default=DEFAULT_SEED_MODEL,
        help="model id the seed config.yaml boots the fleet on "
             f"(default: {DEFAULT_SEED_MODEL}). Ignored if config.yaml exists.",
    )
    args = ap.parse_args()
    if args.repoint_shims:
        py = args.python or Path(sys.executable)
        n = repoint_shims(args.root, py)
        print(f"repointed {n} shim(s) at {py}")
        return 0
    if args.bundle_mode:
        # GUI launch: no uv, no TTY. Lay out, seed, mark — that's all the app
        # needs. No expose_pai_command / paisetup chaining (no shell, no TTY).
        # The .app's model-setup screen passes the owner's provider/model the
        # same way install.sh does, so the seed config.yaml boots on the model
        # they picked (and only that provider's key is needed). Ignored once
        # config.yaml exists.
        lay_out(args.root, bundle_mode=True, seed=args.seed,
                default_provider=args.default_provider,
                default_model=args.default_model)
        print(f"FHS skeleton ready at {args.root} (bundle mode)")
        return 0
    _ensure_uv()
    lay_out(args.root, default_provider=args.default_provider,
            default_model=args.default_model)
    expose_pai_command(args.root)
    print(f"FHS skeleton ready at {args.root}")
    if not args.no_setup:
        maybe_chain_paisetup(args.root)
    return 0


if __name__ == "__main__":
    sys.exit(main())
