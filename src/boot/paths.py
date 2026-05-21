"""Single source of truth for filesystem paths.

`PAI_ROOT` is the FHS root. Override with the `PAI_ROOT` env var; otherwise
defaults to the repo root, which preserves the v0 flat-layout behavior
(`home/` directly under repo root) until the v3 migration moves files
into FHS slots.

Forward-looking v3 helpers (`home_pai`, `var_lib_memory`, etc.) live here
too; they begin to take effect as later phases lay out and populate the
quasi-Linux tree at `$PAI_ROOT`.
"""

from __future__ import annotations

import os
from pathlib import Path


def _default_root() -> Path:
    return Path.home() / ".pai"


PAI_ROOT: Path = Path(os.environ.get("PAI_ROOT", str(_default_root())))

# Repo location — kept distinct from PAI_ROOT for prompt-file resolution
# against the source tree (e.g. src/prompts/*.md).
REPO_ROOT: Path = Path(__file__).resolve().parent.parent.parent

# Default PAI's stitched home (v3). The legacy single global HOME_DIR
# points at `/home/pai/`; multi-PAI launches will pass per-PAI homes
# explicitly via `home_pai(name)`.
DEFAULT_PAI: str = os.environ.get("PAI_NAME", "pai")
HOME_DIR: Path = PAI_ROOT / "home" / DEFAULT_PAI
PROC_DIR: Path = PAI_ROOT / "proc"
EVENTS_DIR: Path = PAI_ROOT / "run" / "pai" / "events"
# Delivery-ack files for send-message. Kept out of EVENTS_DIR so the kernel
# loop doesn't consume them as events; senders poll a per-msg path here.
ACKS_DIR: Path = PAI_ROOT / "run" / "pai" / "acks"


# PATH wiring — the PAI bin dirs the kernel and every subprocess it spawns
# need on PATH (paictl, send-message, CoreLocationCLI, the FHS python, …).
def pai_path_prefix() -> str:
    """The PAI bin dirs joined for prepending to PATH, in priority order:
    venv/bin (FHS python + console scripts), usr/bin (PAI-callable tool
    shims), sbin (owner/kernel tools). bash_tool / shell_tool mirror this
    when building a tool subprocess env."""
    return os.pathsep.join([
        str(PAI_ROOT / "usr" / "lib" / "venv" / "bin"),
        str(PAI_ROOT / "usr" / "bin"),
        str(PAI_ROOT / "sbin"),
    ])


def prepend_pai_path() -> None:
    """Idempotently prepend the PAI bin dirs to os.environ['PATH'].

    A Finder-launched .app inherits no shell PATH, so the kernel — and every
    child it spawns (supervisor services, boot hooks, the per-turn header
    helpers) — would otherwise not find the PAI tools. Called once at kernel
    boot; children inherit the result through os.environ."""
    prefix = pai_path_prefix()
    current = os.environ.get("PATH", "")
    if current == prefix or current.startswith(prefix + os.pathsep):
        return  # already prepended (e.g. a re-exec inheriting our env)
    os.environ["PATH"] = prefix + (os.pathsep + current if current else "")


# v3 FHS helpers — wired up incrementally by later phases.
def etc() -> Path:
    return PAI_ROOT / "etc"


def etc_prompts() -> Path:
    return etc() / "prompts"


def home_pai(name: str) -> Path:
    return PAI_ROOT / "home" / name


def root_home() -> Path:
    return PAI_ROOT / "root"


def var_lib_memory() -> Path:
    return PAI_ROOT / "var" / "lib" / "memory"


def var_lib_instance(name: str) -> Path:
    return PAI_ROOT / "var" / "lib" / "instances" / name


def var_lib_packages() -> Path:
    return PAI_ROOT / "var" / "lib" / "packages"


def var_spool_communication() -> Path:
    return PAI_ROOT / "var" / "spool" / "communication"


def var_spool_messages() -> Path:
    return PAI_ROOT / "var" / "spool" / "communication" / "messages"


def var_spool_email() -> Path:
    return PAI_ROOT / "var" / "spool" / "communication" / "email"


def var_spool_email_drafts() -> Path:
    return var_spool_email() / "drafts"


def var_log() -> Path:
    return PAI_ROOT / "var" / "log"


def proc(name: str) -> Path:
    return PAI_ROOT / "proc" / name


def run_pais(name: str) -> Path:
    return PAI_ROOT / "run" / "pais" / name


def usr_bin() -> Path:
    return PAI_ROOT / "usr" / "bin"


def sbin() -> Path:
    return PAI_ROOT / "sbin"


def usr_lib() -> Path:
    return PAI_ROOT / "usr" / "lib"


def usr_libexec() -> Path:
    return PAI_ROOT / "usr" / "libexec"


def usr_lib_drivers() -> Path:
    return PAI_ROOT / "usr" / "lib" / "drivers"


def usr_lib_skills() -> Path:
    return PAI_ROOT / "usr" / "lib" / "skills"


def usr_lib_pais() -> Path:
    return PAI_ROOT / "usr" / "lib" / "pais"


def usr_lib_subagents() -> Path:
    return PAI_ROOT / "usr" / "lib" / "subagents"


def usr_share_prompts() -> Path:
    return PAI_ROOT / "usr" / "share" / "prompts"


def usr_share_doc() -> Path:
    return PAI_ROOT / "usr" / "share" / "doc"


def usr_src() -> Path:
    return PAI_ROOT / "usr" / "src"


def opt(pkg: str, version: str) -> Path:
    return PAI_ROOT / "opt" / pkg / version


def opt_paiman() -> Path:
    return PAI_ROOT / "opt" / "paiman"


def var_lib_paiman() -> Path:
    return PAI_ROOT / "var" / "lib" / "paiman"
