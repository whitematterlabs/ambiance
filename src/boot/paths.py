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
import shutil
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
HOST_SYSTEM_PATH_DIRS: tuple[str, ...] = (
    "/usr/bin",
    "/bin",
    "/usr/sbin",
    "/sbin",
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "/opt/homebrew/sbin",
    "/usr/local/sbin",
)


def pai_path_entries(root: Path | None = None) -> list[str]:
    """PAI bin dirs in priority order."""
    root = root or PAI_ROOT
    return [
        str(root / "usr" / "lib" / "venv" / "bin"),
        str(root / "usr" / "bin"),
        str(root / "sbin"),
    ]


def _dedupe_path(entries: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        if not entry or entry in seen:
            continue
        seen.add(entry)
        out.append(entry)
    return out


def pai_path_prefix(root: Path | None = None) -> str:
    """The PAI bin dirs joined for prepending to PATH."""
    return os.pathsep.join(pai_path_entries(root))


def build_pai_path(
    current: str | None = None,
    *,
    root: Path | None = None,
    host_first: bool = False,
) -> str:
    """Return PATH with PAI dirs first and host system dirs guaranteed.

    Finder-launched macOS apps can inherit a narrow or empty PATH. Including
    the host defaults keeps real system binaries such as /bin/ps and
    /usr/sbin/lsof reachable.

    Kernel/service environments keep PAI tools first so internal helpers like
    `paictl` and `send-message` resolve without qualification. PAI-facing
    shells pass host_first=True so generic Unix names (`ps`, `clear`, `cal`)
    resolve to macOS; PAI tools remain reachable as `bin/<name>` from the
    stitched home.
    """
    if current is None:
        current = os.environ.get("PATH", "")
    current_entries = current.split(os.pathsep)
    pai_entries = pai_path_entries(root)
    host_entries = list(HOST_SYSTEM_PATH_DIRS)
    if host_first:
        entries = [pai_entries[0]] + host_entries + pai_entries[1:] + current_entries
    else:
        entries = pai_entries + current_entries + host_entries
    return os.pathsep.join(_dedupe_path(entries))


def host_executable(name: str) -> str | None:
    """Resolve a real host executable without consulting PAI/repo venv shims."""
    return shutil.which(name, path=os.pathsep.join(HOST_SYSTEM_PATH_DIRS))


def prepend_pai_path() -> None:
    """Idempotently prepend the PAI bin dirs to os.environ['PATH'].

    A Finder-launched .app inherits no shell PATH, so the kernel — and every
    child it spawns (supervisor services, boot hooks, the per-turn header
    helpers) — would otherwise not find the PAI tools. Called once at kernel
    boot; children inherit the result through os.environ."""
    os.environ["PATH"] = build_pai_path(os.environ.get("PATH", ""))


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


def var_lib_instance_skills(name: str) -> Path:
    """A PAI's private writable skills overlay — self-written skills only that
    PAI sees, stitched into its `home/memory/skills/` view over the read-only
    `/usr/lib/skills/` baseline."""
    return var_lib_instance(name) / "skills"


def var_lib_skills() -> Path:
    """Fleet-shared writable skills overlay — self-written skills every PAI
    sees, stitched into each `home/memory/skills/` view over the read-only
    `/usr/lib/skills/` baseline."""
    return PAI_ROOT / "var" / "lib" / "skills"


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


def run() -> Path:
    """The runtime scratch dir ($PAI_ROOT/run/). Holds ephemeral, non-committed
    state: the event/ack spool, per-PAI run dirs, and generated config the
    kernel writes for the children it supervises (e.g. the LiteLLM proxy)."""
    return PAI_ROOT / "run"


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


def venv_python() -> Path:
    """The FHS venv interpreter — the single runtime python that holds both
    pyproject runtime deps (provisioned by paifs-init) and per-package deps
    installed by `paiman` hooks. Bin shims must target this, not
    `sys.executable`, which on a fresh install is a throwaway clone venv that
    lacks hook-installed deps and vanishes when the clone is removed."""
    return PAI_ROOT / "usr" / "lib" / "venv" / "bin" / "python"


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
