"""Runtime build identity — which release a running process's code came from.

Ground truth is the process's own ``__file__``: when installed, kernel and web
code live under ``$PAI_ROOT/opt/pai/<ver>/src/...``, so the version is
recoverable from any module's path and cannot drift from a marker file or the
``current`` symlink. Dev/git checkouts (code outside ``opt/pai``) report
version ``"dev"`` plus the git HEAD.

This module is the single source every skew comparison uses, plus the pure
policy functions that decide whether the web surface should auto-reboot a
stale kernel (see ``classify_skew`` / ``decide_heal``). Keeping the policy here
(not in the browser) keeps it host-testable; the client only renders the
verdict the hub ships it.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import paths


@dataclass(frozen=True)
class Build:
    version: str
    sha: Optional[str]
    dev: bool

    def as_dict(self) -> dict:
        return asdict(self)


def _version_from_path(file: Path) -> Optional[str]:
    """Return ``<ver>`` if ``file`` lives under ``.../opt/pai/<ver>/...``.

    The path is resolved first, so a launch through ``opt/pai/current/...``
    (a symlink) collapses to the concrete version dir before matching."""
    parts = file.resolve().parts
    for i in range(len(parts) - 2):
        if parts[i] == "opt" and parts[i + 1] == "pai":
            ver = parts[i + 2]
            if ver != "current":
                return ver
    return None


def _installed_sha() -> Optional[str]:
    try:
        return (paths.PAI_ROOT / "var" / "lib" / ".release.sha256").read_text().strip() or None
    except OSError:
        return None


def _git_head() -> Optional[str]:
    try:
        out = subprocess.run(
            ["git", "-C", str(paths.REPO_ROOT), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    sha = out.stdout.strip()
    return sha or None


def running_build() -> Build:
    """The build the *calling process's* code was loaded from — ground truth."""
    ver = _version_from_path(Path(__file__))
    if ver is not None:
        return Build(version=ver, sha=_installed_sha(), dev=False)
    return Build(version="dev", sha=_git_head(), dev=True)


def current_release() -> str:
    """The installed/target build: what ``opt/pai/current`` points at (or the
    ``.release`` marker), i.e. what a freshly (re-)started process would load.
    ``"dev"`` when neither exists (git checkout)."""
    link = paths.PAI_ROOT / "opt" / "pai" / "current"
    try:
        target = link.resolve(strict=True)
        if target.name and target.name != "current":
            return target.name
    except OSError:
        pass
    try:
        marker = (paths.PAI_ROOT / "var" / "lib" / ".release").read_text().strip()
        if marker:
            return marker
    except OSError:
        pass
    return "dev"


# --- kernel build stamp -----------------------------------------------------


def kernel_stamp_path() -> Path:
    return paths.PAI_ROOT / "run" / "pai" / "build" / "kernel.json"


def write_kernel_stamp(pid: int) -> None:
    """Record the running kernel's build at boot. Re-exec (``kernel:restart``)
    re-runs boot, so an in-place restart restamps to the new build for free."""
    b = running_build()
    data = {**b.as_dict(), "pid": pid, "started": datetime.now().isoformat(timespec="seconds")}
    p = kernel_stamp_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(data))
    tmp.replace(p)


def read_kernel_stamp() -> Optional[dict]:
    try:
        return json.loads(kernel_stamp_path().read_text())
    except (OSError, ValueError):
        return None


# --- skew classification + heal policy (pure) -------------------------------


def classify_skew(kernel: Optional[str], console: str, current: str) -> str:
    """Stateless verdict from three version strings.

    Returns one of: ``unknown`` (no kernel stamp yet), ``in_sync``,
    ``kernel_stale`` (console current, kernel behind — the failure we hit),
    ``console_stale`` (kernel current, console behind), ``both_stale``."""
    if kernel is None:
        return "unknown"
    if kernel == console:
        return "in_sync"
    kernel_current = kernel == current
    console_current = console == current
    if console_current and not kernel_current:
        return "kernel_stale"
    if kernel_current and not console_current:
        return "console_stale"
    return "both_stale"


@dataclass
class HealState:
    last_kernel_ver: Optional[str] = None
    last_attempt_monotonic: Optional[float] = None
    escalated: bool = False


# Environment marker for a console self re-exec: set to the release being
# adopted just before the web surface replaces its own process image. The
# environment survives the exec, so a restart that *didn't* pick up the new
# build (still stale for the same release) degrades to the banner instead of
# exec-looping.
CONSOLE_REEXEC_ENV = "PAI_CONSOLE_RESTARTED_FOR"


def decide_console_restart(
    console: str,
    current: str,
    *,
    dev: bool,
    already: str | None,
    can_restart: bool,
) -> bool:
    """Whether a stale console should replace itself with a fresh process.

    Rebooting the kernel can't heal a stale console: after `pai update` swaps
    the release dir, this process still runs the old ``pai_web`` code with
    paths into the wiped dir (404s on `/` and on any new `/api/*` route). Only
    a re-exec of the serving process fixes that. Pure — the caller sets the
    ``already`` env marker and performs the exec. Restricted to release-built
    consoles targeting a release build (dev checkouts are the developer's
    business), one attempt per release."""
    if not can_restart or dev or current == "dev":
        return False
    if already == current:
        return False
    return console != current


def decide_heal(
    kernel: Optional[str],
    console: str,
    current: str,
    state: HealState,
    now: float,
    cooldown: float = 60.0,
) -> str:
    """Action for the web surface given the current builds and prior attempts.

    Returns ``none`` | ``reboot`` | ``escalate`` | ``warn_console``. Pure — the
    caller updates ``state`` based on the action it takes.

    - Kernel behind ``current`` → reboot it (once), then wait out ``cooldown``;
      if it's *still* the same stale build after the cooldown, ``escalate`` to a
      manual banner (the auto-reboot didn't take).
    - Console behind ``current`` → ``warn_console``; rebooting the kernel can't
      fix a stale console, so never auto-act."""
    if kernel is None or kernel == console:
        return "none"
    if kernel != current:
        if (
            state.last_kernel_ver == kernel
            and state.last_attempt_monotonic is not None
        ):
            if now - state.last_attempt_monotonic < cooldown:
                return "none"
            return "escalate"
        return "reboot"
    # kernel == current, but console != kernel → console is the stale one.
    return "warn_console"
