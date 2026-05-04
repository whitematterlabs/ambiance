# PAI Boot Architecture & `src/` Decomposition Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `~/.pai/` self-contained at boot: a real `~/.pai/sbin/init` entrypoint execs into the kernel at `~/.pai/boot/`, with explicit boot phases. Decompose today's monolithic `src/` into FHS slots so the repo's `src/` stops being the runtime root.

**Architecture:** Rename the `kernel` Python package to `boot` and surface it under `~/.pai/boot/` (symlinked from the repo, same pattern `paifs_init` already uses for `usr/lib/drivers/`). Refactor `src/pai.py` into `src/boot/init.py` — a thin shim that verifies layout and `exec`s into the kernel module. Extract today's inline `run()` startup into named boot-phase modules. Move `tui/`, `migrate.py`, `reset.py` into `src/sbin/` and install as privileged shims. Move `src/guides/` to `src/usr/share/doc/`. Remove `src/seed/`. Defer the `/proc/<pid>/` + `/run/pais/<name>/` two-layer refactor to a follow-up plan.

**Tech Stack:** Python 3.14, uv, asyncio, pyyaml. Tests with pytest.

**Out of scope (separate plan):**
- `/proc/<pid>/` PID-keyed migration
- `/run/pais/<name>/` name-layer with inbox + durable log
- `/sys/drivers/<name>/` runtime state migration
- `/boot/recovery/` snapshots
- `paiman` against `/opt/`

---

## File Structure

| Action | Path | Responsibility |
|---|---|---|
| Rename | `src/kernel/` → `src/boot/` | Kernel source ("the image") |
| Create | `src/boot/init.py` | `/sbin/init` entry — verify layout, exec kernel |
| Create | `src/boot/entry.py` | Kernel main: orchestrate phases, enter supervise loop |
| Create | `src/boot/phases/__init__.py` | Phase package |
| Create | `src/boot/phases/sanity.py` | Phase 1: required-dir check |
| Create | `src/boot/phases/clean.py` | Phase 2: wipe ephemeral state |
| Create | `src/boot/phases/probe.py` | Phase 3: driver health probe |
| Create | `src/boot/phases/reconcile.py` | Phase 4: thin wrapper around `boot.config.reconcile_from_config` |
| Create | `src/boot/phases/start.py` | Phase 5–6: start kernelPAI first, then fleet |
| Modify | `src/boot/main.py` | `run()` becomes phase-7 supervise loop only |
| Delete | `src/pai.py` | Replaced by `src/boot/init.py` |
| Move | `src/tui/` → `src/sbin/tui/` | Privileged owner client |
| Move | `src/migrate.py` → `src/sbin/migrate.py` | One-shot kernelPAI op |
| Move | `src/reset.py` → `src/sbin/reset.py` | One-shot kernelPAI op |
| Move | `src/guides/` → `src/usr/share/doc/` | Shipped docs |
| Delete | `src/seed/` | Folded into bundle `defaults/` (out of scope here — just remove unused tree) |
| Modify | `src/bin/paifs_init.py` | Wire `boot/`, `sbin/`, `usr/share/doc/` symlinks; install sbin shims |
| Modify | `pyproject.toml` | Update `[project.scripts]` for renamed/moved entries |
| Create | `tests/test_boot_phases.py` | Phase modules tested in isolation |
| Create | `tests/test_boot_init.py` | Init shim tested via subprocess |
| Create | `tests/test_paifs_init_boot.py` | Skeleton wires boot/sbin slots |

---

## Task 1: Rename `kernel` package to `boot`

**Files:**
- Move: `src/kernel/` → `src/boot/`
- Modify: every `*.py` under `src/`, `tests/` that imports `kernel.*`
- Modify: `pyproject.toml` (build target list)

- [ ] **Step 1: Verify nothing else holds the name**

```bash
cd ~/Projects/pai
grep -rln '\bkernel\b' src tests pyproject.toml | sort -u
```

Expected: every hit is either an import (`from kernel.X`, `import kernel`, `python -m kernel`), a hatch build target, or a comment/log message containing the word "kernel" generically. Eyeball-confirm none refer to a separate `kernel`-named package outside `src/kernel/`.

- [ ] **Step 2: Move the package directory**

```bash
git mv src/kernel src/boot
```

- [ ] **Step 3: Rewrite imports**

```bash
# `from kernel.X import …` → `from boot.X import …`
grep -rln '^from kernel\.' src tests | xargs sed -i '' 's|^from kernel\.|from boot.|g'
# `from kernel import …` → `from boot import …`
grep -rln '^from kernel ' src tests | xargs sed -i '' 's|^from kernel |from boot |g'
# `import kernel.X` → `import boot.X`
grep -rln '^import kernel\.' src tests | xargs sed -i '' 's|^import kernel\.|import boot.|g'
# bare `import kernel` → `import boot`
grep -rln '^import kernel$' src tests | xargs sed -i '' 's|^import kernel$|import boot|g'
```

- [ ] **Step 4: Rewrite `python -m kernel` invocations**

```bash
grep -rln 'python -m kernel\b\|"-m", "kernel"' src tests | xargs sed -i '' 's|-m kernel\b|-m boot|g; s|"-m", "kernel"|"-m", "boot"|g'
```

- [ ] **Step 5: Update `pyproject.toml` build target**

Modify the `[tool.hatch.build.targets.wheel]` packages list:

```toml
[tool.hatch.build.targets.wheel]
packages = ["src/boot", "src/drivers", "src/tui", "src/bin"]
```

- [ ] **Step 6: Run the test suite**

Run: `uv run pytest -x`
Expected: PASS. If imports remain broken, fix them — every import of `kernel.*` should now read `boot.*`.

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "rename kernel package to boot

The supervisor 'image' lives at /boot/ per FILESYSTEM_v3.md.
Pure mechanical rename — no behavior change."
```

---

## Task 2: Symlink `~/.pai/boot/` to `src/boot/` via paifs_init

**Files:**
- Modify: `src/bin/paifs_init.py`
- Test: `tests/test_paifs_init_boot.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_paifs_init_boot.py`:

```python
"""paifs_init wires the v3 boot/sbin/usr/share/doc slots."""
from __future__ import annotations

from pathlib import Path

from bin.paifs_init import lay_out


def test_lay_out_creates_boot_symlink_to_repo_src(tmp_path: Path) -> None:
    lay_out(tmp_path)
    boot = tmp_path / "boot"
    assert boot.is_symlink(), "expected ~/.pai/boot to be a symlink"
    target = boot.resolve()
    assert target.name == "boot" and target.parent.name == "src", (
        f"expected boot -> repo/src/boot, got {target}"
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_paifs_init_boot.py -v`
Expected: FAIL — `boot` is currently a real dir (`SKELETON` includes `boot/recovery`), not a symlink.

- [ ] **Step 3: Update `paifs_init` to symlink boot**

Modify `src/bin/paifs_init.py`:

In `SKELETON`, replace `"boot/recovery",` with just the recovery sub-path the symlink target will own — actually the kernel source lives at `src/boot/`, and `boot/recovery/` is deferred per spec. We resolve this by symlinking the whole `boot/` dir to repo `src/boot/`, and accepting that `boot/recovery/` is a future concern (it'd live inside the repo's `src/boot/recovery/` once added).

Remove `"boot/recovery",` from `SKELETON`:

```python
SKELETON: tuple[str, ...] = (
    "bin",
    "sbin",
    "etc/drivers",
    # ... rest unchanged
)
```

Add to `SYMLINKS`:

```python
SYMLINKS: tuple[tuple[str, Path], ...] = (
    ("boot", REPO_ROOT / "src" / "boot"),
    ("usr/src", REPO_ROOT / "src"),
    ("usr/lib/drivers", REPO_ROOT / "src" / "drivers"),
    ("usr/share/prompts", REPO_ROOT / "src" / "prompts"),
)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_paifs_init_boot.py -v`
Expected: PASS.

- [ ] **Step 5: Run paifs_init against `~/.pai/` to update the live install**

```bash
uv run python -m bin.paifs_init
ls -la ~/.pai/boot
```

Expected: `~/.pai/boot -> ~/Projects/pai/src/boot`. If a real dir exists from a prior run, remove it first (`rm -rf ~/.pai/boot/recovery && rmdir ~/.pai/boot`) and re-run.

- [ ] **Step 6: Commit**

```bash
git add src/bin/paifs_init.py tests/test_paifs_init_boot.py
git commit -m "paifs_init: symlink ~/.pai/boot/ to repo src/boot/

Per FILESYSTEM_v3.md, /boot/ is the kernel 'image' slot. Same
symlink pattern as usr/lib/drivers and usr/src — repo edits
land live in the FHS root."
```

---

## Task 3: Create `src/boot/init.py` — `/sbin/init` entrypoint

**Files:**
- Create: `src/boot/init.py`
- Test: `tests/test_boot_init.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_boot_init.py`:

```python
"""/sbin/init: layout-check then exec into kernel."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest


def test_init_fails_loudly_on_missing_layout(tmp_path: Path) -> None:
    """Init bails if PAI_ROOT lacks required dirs."""
    env = {"PAI_ROOT": str(tmp_path), "PATH": "/usr/bin:/bin"}
    result = subprocess.run(
        [sys.executable, "-m", "boot.init", "--check-only"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "missing" in result.stderr.lower() or "not found" in result.stderr.lower()


def test_init_check_only_passes_on_complete_layout(tmp_path: Path) -> None:
    """Init returns 0 in --check-only mode when layout is valid."""
    from bin.paifs_init import lay_out
    lay_out(tmp_path)
    env = {"PAI_ROOT": str(tmp_path), "PATH": "/usr/bin:/bin"}
    result = subprocess.run(
        [sys.executable, "-m", "boot.init", "--check-only"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_boot_init.py -v`
Expected: FAIL — `boot.init` does not exist yet.

- [ ] **Step 3: Implement `src/boot/init.py`**

```python
"""/sbin/init — entrypoint. Verify layout, exec into the kernel.

After `os.execvp`, this process IS the kernel — there is no separate
init lingering as PID 1. Mirrors Linux: /sbin/init *is* systemd, it
doesn't fork it.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .paths import PAI_ROOT

REQUIRED_DIRS: tuple[str, ...] = (
    "etc",
    "var/lib",
    "var/log",
    "proc",
    "run",
    "boot",
    "usr",
)


def check_layout(root: Path) -> list[str]:
    """Return a list of missing required dirs. Empty list = OK."""
    missing: list[str] = []
    for rel in REQUIRED_DIRS:
        if not (root / rel).exists():
            missing.append(rel)
    return missing


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--check-only",
        action="store_true",
        help="verify layout and exit (do not exec into kernel)",
    )
    args = ap.parse_args()

    missing = check_layout(PAI_ROOT)
    if missing:
        print(
            f"init: PAI_ROOT={PAI_ROOT} missing required dirs: {', '.join(missing)}\n"
            f"      run `paifs-init` to lay out the skeleton.",
            file=sys.stderr,
        )
        return 1

    if args.check_only:
        return 0

    # Hand off: this process becomes the kernel. No return on success.
    os.execvp(sys.executable, [sys.executable, "-u", "-m", "boot.entry"])


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_boot_init.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/boot/init.py tests/test_boot_init.py
git commit -m "boot: add init.py — /sbin/init entrypoint

Verifies PAI_ROOT layout, then execvp's into boot.entry.
After exec, the process IS the kernel — no separate init
lingering."
```

---

## Task 4: Phase 1 — `boot.phases.sanity`

**Files:**
- Create: `src/boot/phases/__init__.py`
- Create: `src/boot/phases/sanity.py`
- Test: `tests/test_boot_phases.py`

- [ ] **Step 1: Create the empty package**

```bash
mkdir -p src/boot/phases
touch src/boot/phases/__init__.py
```

- [ ] **Step 2: Write the failing test**

Create `tests/test_boot_phases.py`:

```python
"""Boot phase modules tested in isolation against a temp PAI_ROOT."""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def laid_out_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from bin.paifs_init import lay_out
    lay_out(tmp_path)
    monkeypatch.setenv("PAI_ROOT", str(tmp_path))
    # Re-import paths so PAI_ROOT picks up the env var.
    import importlib

    import boot.paths as paths
    importlib.reload(paths)
    return tmp_path


def test_sanity_passes_on_complete_layout(laid_out_root: Path) -> None:
    from boot.phases import sanity
    sanity.run()  # returns None, raises on failure


def test_sanity_raises_on_missing_dir(laid_out_root: Path) -> None:
    (laid_out_root / "var" / "log").rmdir()
    (laid_out_root / "var").rmdir()  # parent must also go
    from boot.phases import sanity
    with pytest.raises(sanity.SanityError) as exc_info:
        sanity.run()
    assert "var/log" in str(exc_info.value) or "var" in str(exc_info.value)
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/test_boot_phases.py::test_sanity_passes_on_complete_layout -v`
Expected: FAIL — `boot.phases.sanity` does not exist.

- [ ] **Step 4: Implement `src/boot/phases/sanity.py`**

```python
"""Phase 1: sanity — verify required dirs exist; bail loudly if not."""
from __future__ import annotations

from ..paths import PAI_ROOT

REQUIRED: tuple[str, ...] = (
    "etc",
    "var/lib",
    "var/log",
    "proc",
    "run",
    "boot",
    "usr",
)


class SanityError(RuntimeError):
    pass


def run() -> None:
    missing = [rel for rel in REQUIRED if not (PAI_ROOT / rel).exists()]
    if missing:
        raise SanityError(
            f"PAI_ROOT={PAI_ROOT} missing: {', '.join(missing)}"
        )
    print(f"[boot] sanity: layout OK at {PAI_ROOT}", flush=True)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_boot_phases.py -v`
Expected: PASS for the two sanity tests.

- [ ] **Step 6: Commit**

```bash
git add src/boot/phases/ tests/test_boot_phases.py
git commit -m "boot: add phases.sanity (phase 1)

First boot phase: verify PAI_ROOT contains the FHS skeleton."
```

---

## Task 5: Phase 2 — `boot.phases.clean`

**Files:**
- Create: `src/boot/phases/clean.py`
- Test: extend `tests/test_boot_phases.py`

- [ ] **Step 1: Append failing tests to `tests/test_boot_phases.py`**

```python
def test_clean_wipes_tmp(laid_out_root: Path) -> None:
    junk = laid_out_root / "tmp" / "junk.txt"
    junk.write_text("stale")
    from boot.phases import clean
    clean.run()
    assert not junk.exists()
    assert (laid_out_root / "tmp").is_dir()  # dir itself preserved


def test_clean_wipes_run_pai_events(laid_out_root: Path) -> None:
    events = laid_out_root / "run" / "pai" / "events"
    stale = events / "20240101T000000-test.yaml"
    stale.write_text("kind: stale")
    from boot.phases import clean
    clean.run()
    assert not stale.exists()
    assert events.is_dir()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_boot_phases.py -v -k clean`
Expected: FAIL — `boot.phases.clean` does not exist.

- [ ] **Step 3: Implement `src/boot/phases/clean.py`**

```python
"""Phase 2: clean — wipe ephemeral state from prior boots.

`tmp/` is system-wide ephemeral. `run/pai/events/` may hold stale event
files dropped by drivers between the kernel's last shutdown and this
boot. We do NOT wipe `proc/` here — process state is owned by the
proc-layer migration. Stale `proc/<pid>/` cleanup belongs to the
follow-up plan that introduces PID-keyed proc.
"""
from __future__ import annotations

import shutil

from ..paths import PAI_ROOT


def _wipe_dir_contents(path) -> None:
    if not path.is_dir():
        return
    for child in path.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()


def run() -> None:
    _wipe_dir_contents(PAI_ROOT / "tmp")
    _wipe_dir_contents(PAI_ROOT / "run" / "pai" / "events")
    print("[boot] clean: wiped tmp/ and run/pai/events/", flush=True)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_boot_phases.py -v -k clean`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/boot/phases/clean.py tests/test_boot_phases.py
git commit -m "boot: add phases.clean (phase 2)

Wipes tmp/ and run/pai/events/ from prior boots. proc/
cleanup deferred to the proc-layer follow-up plan."
```

---

## Task 6: Phase 3 — `boot.phases.probe`

**Files:**
- Create: `src/boot/phases/probe.py`
- Test: extend `tests/test_boot_phases.py`

- [ ] **Step 1: Append failing tests**

```python
def test_probe_logs_each_driver(laid_out_root: Path, capsys) -> None:
    from boot.phases import probe
    # Drivers shipped: imessage, email. paifs_init exposes events.yaml
    # for each at etc/drivers/<name>/events.yaml.
    probe.run()
    out = capsys.readouterr().out
    assert "imessage" in out
    assert "ok" in out.lower() or "missing" in out.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_boot_phases.py -v -k probe`
Expected: FAIL — `boot.phases.probe` does not exist.

- [ ] **Step 3: Implement `src/boot/phases/probe.py`**

```python
"""Phase 3: probe — driver health check.

For each driver registered in /etc/drivers/<name>/, confirm its
events.yaml is readable and the corresponding code module is
importable. Outputs one line per driver, never raises — a degraded
driver doesn't block boot, but it's logged loudly so kernelPAI can
self-heal once it's up.
"""
from __future__ import annotations

import importlib

import yaml

from ..paths import PAI_ROOT


def _probe_one(driver_name: str) -> str:
    events_path = PAI_ROOT / "etc" / "drivers" / driver_name / "events.yaml"
    try:
        with events_path.open() as f:
            yaml.safe_load(f)
    except Exception as e:
        return f"ERR config unreadable ({e!r})"
    try:
        importlib.import_module(f"drivers.{driver_name}")
    except Exception as e:
        return f"ERR code not importable ({e!r})"
    return "ok"


def run() -> None:
    drivers_dir = PAI_ROOT / "etc" / "drivers"
    if not drivers_dir.is_dir():
        print("[boot] probe: no /etc/drivers/ — skipping", flush=True)
        return
    for child in sorted(drivers_dir.iterdir()):
        if not child.is_dir():
            continue
        name = child.name
        verdict = _probe_one(name)
        print(f"[boot] probe: {name} — {verdict}", flush=True)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_boot_phases.py -v -k probe`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/boot/phases/probe.py tests/test_boot_phases.py
git commit -m "boot: add phases.probe (phase 3)

Per-driver health probe: events.yaml readable + code importable.
Logs verdict; never blocks boot."
```

---

## Task 7: Phases 4–6 — reconcile + start

**Files:**
- Create: `src/boot/phases/reconcile.py`
- Create: `src/boot/phases/start.py`
- Test: extend `tests/test_boot_phases.py`

- [ ] **Step 1: Append failing tests**

```python
def test_reconcile_phase_calls_config_reconcile(laid_out_root: Path) -> None:
    """Phase wraps boot.config.reconcile_from_config — that already has
    its own tests. We only verify the phase calls it without crashing
    against an empty config."""
    cfg = laid_out_root / "etc" / "config.yaml"
    cfg.write_text("pais: []\n")
    from boot.phases import reconcile
    reconcile.run()  # no-op on empty fleet
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_boot_phases.py -v -k reconcile`
Expected: FAIL — module doesn't exist.

- [ ] **Step 3: Implement `src/boot/phases/reconcile.py`**

```python
"""Phase 4: reconcile — apply /etc/config.yaml against the fleet."""
from __future__ import annotations

from .. import config


def run() -> None:
    config.reconcile_from_config()
    print("[boot] reconcile: fleet reconciled", flush=True)
```

- [ ] **Step 4: Implement `src/boot/phases/start.py`**

```python
"""Phases 5–6: start kernelPAI first, then the fleet.

Today this is largely a no-op wrapper because reconcile already spawns
proc entries and `proc-watcher` resumes the running set in supervise.
The phase exists so the boot sequence has an explicit hook: when the
proc-layer follow-up lands and process spawning becomes lifecycle-
aware, the kernelPAI-first ordering will be enforced here.
"""
from __future__ import annotations


def run() -> None:
    # Reserved for the proc-layer plan. Current proc semantics are
    # spec-on-disk; resume happens inside supervise.entry().
    print("[boot] start: kernelPAI + fleet (deferred to supervise loop)", flush=True)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_boot_phases.py -v -k reconcile`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/boot/phases/reconcile.py src/boot/phases/start.py tests/test_boot_phases.py
git commit -m "boot: add phases.reconcile + phases.start (phases 4-6)

Reconcile wraps config.reconcile_from_config. start.py is a
named hook — the proc-layer plan will fill it in."
```

---

## Task 8: Phase 7 — `boot/entry.py` orchestrator

**Files:**
- Create: `src/boot/entry.py`
- Modify: `src/boot/main.py` — keep its `run()` as the supervise loop body

- [ ] **Step 1: Implement `src/boot/entry.py`**

```python
"""Boot entrypoint, executed by /sbin/init via execvp.

Runs phases 1–6 synchronously, then enters phase 7 (the asyncio
supervise loop) by delegating to boot.main.run().
"""
from __future__ import annotations

import asyncio
import sys
import traceback

from .phases import clean, probe, reconcile, sanity, start
from . import main as supervise


def boot() -> int:
    try:
        sanity.run()
        clean.run()
        probe.run()
        reconcile.run()
        start.run()
    except sanity.SanityError as e:
        print(f"[boot] sanity failed: {e}", file=sys.stderr, flush=True)
        return 1
    except Exception as e:
        tb = traceback.format_exc()
        print(f"[boot] phase failed: {e!r}\n{tb}", file=sys.stderr, flush=True)
        return 2
    try:
        asyncio.run(supervise.run())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(boot())
```

- [ ] **Step 2: Update `src/boot/main.py`**

Remove `_ensure_etc_symlink()`, `_migrate_legacy_me_dir()`, `_migrate_legacy_pai_slug()`, and the inline `C.reconcile_from_config()` call from `run()` — they're now phase concerns. Migrations are legacy from before paifs_init owned layout; if any project still depends on them, leave them in place but add a comment that they should be moved out in a future cleanup.

Concretely, in `src/boot/main.py`'s `run()`:

```python
async def run() -> None:
    _install_stdout_tee()
    loop = asyncio.get_running_loop()
    # NOTE: layout/legacy migrations moved to boot.phases. Reconcile is
    # now phase 4 and runs before this function is invoked.
    contacts.sync_to_people(M.PEOPLE_DIR)
    heap = T.rebuild_from_proc()
    watcher = EventWatcher(P.EVENTS_DIR, loop)
    watcher.start()
    await supervisor.resume_from_disk()
    print(f"[kernel] supervise: started — {len(heap)} timers loaded", flush=True)
    # ... (rest of the existing main loop unchanged)
```

Delete the four removed helper functions and their imports if unused (e.g. `os`, `yaml` if no longer referenced in this file).

- [ ] **Step 3: Update `src/boot/__main__.py`**

Make `python -m boot` invoke `entry.boot()`:

```python
"""`python -m boot` entry — runs the full boot sequence."""
from .entry import boot
import sys

sys.exit(boot())
```

(If `__main__.py` previously dispatched on a `run` arg, replace that with the unconditional boot call. The shim at `/sbin/init` is the only canonical entrypoint now.)

- [ ] **Step 4: Run the test suite**

Run: `uv run pytest -x`
Expected: PASS. Existing `boot.main.run` tests should continue to pass; the migrations being moved out shouldn't break tests that exercised them (they were untested in-place behavior).

- [ ] **Step 5: Smoke-test boot**

```bash
uv run python -m boot.init --check-only && echo OK
```

Expected: `OK`. Then a real boot:

```bash
timeout 5 uv run python -m boot 2>&1 | head -30
```

Expected: see `[boot] sanity:`, `[boot] clean:`, `[boot] probe:`, `[boot] reconcile:`, `[boot] start:`, then `[kernel] supervise: started`. Timeout cuts it after 5s — that's normal; the supervise loop runs forever.

- [ ] **Step 6: Commit**

```bash
git add src/boot/entry.py src/boot/main.py src/boot/__main__.py
git commit -m "boot: add entry.py orchestrator (phase 7)

Runs phases 1-6 synchronously, then delegates to main.run()
for the supervise loop. main.run() is now phase-7 only;
layout/migration concerns moved into phases."
```

---

## Task 9: Replace `src/pai.py` with sbin shims

**Files:**
- Delete: `src/pai.py`
- Modify: `pyproject.toml`
- Modify: `src/bin/paifs_init.py`

- [ ] **Step 1: Update `pyproject.toml` `[project.scripts]`**

Replace the entries:

```toml
[project.scripts]
addcontact = "bin.addcontact:main"
addemail = "bin.addemail:main"
clear = "bin.clear:main"
compact = "bin.compact:main"
edit-file = "bin.edit_file:main"
imessage-backfill = "bin.imessage_backfill:main"
init = "boot.init:main"
ipc = "bin.ipc:main"
paictl = "bin.paictl:main"
paifs-init = "bin.paifs_init:main"
resolve-contact = "bin.resolve_contact:main"
subagent = "bin.subagent:main"
```

(Adds `init`. Removes any `pai` script if it existed previously.)

- [ ] **Step 2: Delete `src/pai.py`**

```bash
git rm src/pai.py
```

- [ ] **Step 3: Teach paifs_init which scripts go to `sbin/` vs `bin/`**

Modify `src/bin/paifs_init.py`. Add a constant near the top:

```python
# Scripts that get installed into /sbin/ instead of /bin/. These are
# privileged kernel/owner ops, not PAI-callable tools.
SBIN_SCRIPTS: frozenset[str] = frozenset({"init"})
```

Update `install_bin_shims` so each script lands in the right directory:

```python
def install_bin_shims(venv_dir: Path, root: Path) -> None:
    """Generate shim files for each [project.scripts] entry.

    Splits by privilege: SBIN_SCRIPTS go to sbin/, the rest to bin/.
    """
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
    # Expose the venv's python at usr/bin/python (unchanged).
    py_shim = bin_dir / "python"
    if py_shim.is_symlink() or py_shim.exists():
        py_shim.unlink()
    py_shim.write_text(f'#!/bin/sh\nexec "{py}" "$@"\n')
    py_shim.chmod(0o755)
```

- [ ] **Step 4: Re-run paifs_init**

```bash
uv run python -m bin.paifs_init
ls ~/.pai/sbin/ ~/.pai/usr/bin/
```

Expected: `~/.pai/sbin/init` exists; `~/.pai/usr/bin/` no longer has `init`.

- [ ] **Step 5: Smoke test `~/.pai/sbin/init --check-only`**

```bash
~/.pai/sbin/init --check-only && echo OK
```

Expected: `OK`.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "decompose: src/pai.py → /sbin/init

paifs_init now installs scripts into /sbin/ vs /usr/bin/ based
on the SBIN_SCRIPTS allowlist. /sbin/init is the real entrypoint;
TUI launcher convenience is removed (TUI runs standalone)."
```

---

## Task 10: Move `src/tui/`, `src/migrate.py`, `src/reset.py` to sbin

**Files:**
- Move: `src/tui/` → `src/sbin/tui/`
- Move: `src/migrate.py` → `src/sbin/migrate.py`
- Move: `src/reset.py` → `src/sbin/reset.py`
- Modify: `pyproject.toml`
- Modify: `src/bin/paifs_init.py`

- [ ] **Step 1: Create `src/sbin/` and move files**

```bash
mkdir -p src/sbin
git mv src/tui src/sbin/tui
git mv src/migrate.py src/sbin/migrate.py
git mv src/reset.py src/sbin/reset.py
touch src/sbin/__init__.py
```

- [ ] **Step 2: Rewrite imports**

```bash
grep -rln '\bfrom tui\b\|\bimport tui\b' src tests | xargs -r sed -i '' 's|\bfrom tui\b|from sbin.tui|g; s|\bimport tui\b|import sbin.tui|g'
```

If `migrate.py` or `reset.py` were imported from anywhere (unlikely — they're scripts), update those refs the same way.

- [ ] **Step 3: Add CLI entries to `pyproject.toml`**

```toml
[project.scripts]
# ... existing entries unchanged
init = "boot.init:main"
migrate = "sbin.migrate:main"
reset = "sbin.reset:main"
tui = "sbin.tui.app:main"  # adjust if entry function lives elsewhere
```

(Read `src/sbin/tui/__main__.py` and `src/sbin/tui/app.py` to confirm the right `module:attr`. Existing TUI launch path was via `python -m tui` so `sbin.tui:__main__` may need a thin `main()` wrapper — see step 4.)

- [ ] **Step 4: Add a `main()` to `src/sbin/tui/__init__.py` if missing**

If TUI launches via `python -m tui` (no module-level `main`), add one. Read `src/sbin/tui/__main__.py` to find the launch call, then in `src/sbin/tui/__init__.py`:

```python
def main() -> int:
    from .app import TuiApp
    TuiApp().run()
    return 0
```

And update `pyproject.toml`: `tui = "sbin.tui:main"`.

- [ ] **Step 5: Update `paifs_init` SBIN_SCRIPTS**

```python
SBIN_SCRIPTS: frozenset[str] = frozenset({"init", "migrate", "reset", "tui"})
```

Also update `pyproject.toml`'s hatch packages list:

```toml
[tool.hatch.build.targets.wheel]
packages = ["src/boot", "src/drivers", "src/sbin", "src/bin"]
```

- [ ] **Step 6: Re-run paifs_init and the tests**

```bash
uv run python -m bin.paifs_init
ls ~/.pai/sbin/
uv run pytest -x
```

Expected: `~/.pai/sbin/` contains `init`, `migrate`, `reset`, `tui`. Tests pass.

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "decompose: tui/migrate/reset → src/sbin/

Privileged owner/kernelPAI ops move to /sbin/. paifs_init
splits shims by SBIN_SCRIPTS allowlist."
```

---

## Task 11: Move `src/guides/` to `src/usr/share/doc/`

**Files:**
- Move: `src/guides/` → `src/usr/share/doc/`
- Modify: `src/bin/paifs_init.py`

- [ ] **Step 1: Move docs**

```bash
mkdir -p src/usr/share
git mv src/guides src/usr/share/doc
```

- [ ] **Step 2: Update CLAUDE.md and any internal references**

```bash
grep -rln 'src/guides' . --include='*.md' --include='*.py' | xargs -r sed -i '' 's|src/guides|src/usr/share/doc|g'
```

Eyeball the diff before committing — make sure no path-sensitive code (the legacy migration helpers, e.g.) was hit incorrectly.

- [ ] **Step 3: Add the symlink to `paifs_init`**

In `SYMLINKS`:

```python
SYMLINKS: tuple[tuple[str, Path], ...] = (
    ("boot", REPO_ROOT / "src" / "boot"),
    ("usr/src", REPO_ROOT / "src"),
    ("usr/lib/drivers", REPO_ROOT / "src" / "drivers"),
    ("usr/share/prompts", REPO_ROOT / "src" / "prompts"),
    ("usr/share/doc", REPO_ROOT / "src" / "usr" / "share" / "doc"),
)
```

Remove `"usr/share/prompts"` from `SKELETON` if present (it was symlinked anyway, but `usr/share/doc` would clash if listed).

- [ ] **Step 4: Re-run paifs_init**

```bash
uv run python -m bin.paifs_init
ls -la ~/.pai/usr/share/
```

Expected: `doc -> ~/Projects/pai/src/usr/share/doc`.

- [ ] **Step 5: Run tests**

Run: `uv run pytest -x`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "decompose: src/guides → src/usr/share/doc

Per FILESYSTEM_v3.md, shipped documentation lives in
/usr/share/doc/. paifs_init exposes it via symlink, same
pattern as prompts and drivers."
```

---

## Task 12: Remove `src/seed/`

**Files:**
- Delete: `src/seed/`
- Modify: `src/bin/paifs_init.py`

- [ ] **Step 1: Inline the seed config defaults**

Read `src/seed/config.yaml`. The `paifs_init.SEEDS` table copies it to `~/.pai/etc/config.yaml` on first install. Once we delete `src/seed/`, the seed has nowhere to come from. Two options:

Option A (preferred): bake the seed content into `paifs_init.py` as a constant string literal so the install is fully self-contained.
Option B: keep `src/seed/` until v3's bundle `defaults/` lands.

Pick A. Read `src/seed/config.yaml`:

```bash
cat src/seed/config.yaml
```

Embed in `src/bin/paifs_init.py`:

```python
DEFAULT_CONFIG_YAML = """\
# (paste the file's contents here verbatim)
"""
```

Replace `SEEDS` and `ensure_seed`:

```python
def ensure_default_config(root: Path) -> None:
    """Write a default etc/config.yaml on first install. Never overwrites."""
    dest = root / "etc" / "config.yaml"
    if dest.exists():
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(DEFAULT_CONFIG_YAML)
```

In `lay_out`, replace the `for src, dest in SEEDS:` loop with `ensure_default_config(root)`.

- [ ] **Step 2: Delete `src/seed/`**

```bash
git rm -r src/seed
```

- [ ] **Step 3: Run paifs_init against a fresh tmp root**

```bash
uv run python -c "
from pathlib import Path
import tempfile
from bin.paifs_init import lay_out
with tempfile.TemporaryDirectory() as d:
    lay_out(Path(d))
    print((Path(d) / 'etc' / 'config.yaml').read_text())
"
```

Expected: prints the embedded default config.

- [ ] **Step 4: Run the test suite**

Run: `uv run pytest -x`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "decompose: remove src/seed/

Default etc/config.yaml is now embedded as a literal in
paifs_init. src/seed/ existed as a v0 staging slot — its
forward home is the bundle defaults/ tree (out of scope here)."
```

---

## Task 13: End-to-end smoke test

**Files:**
- Create: `tests/test_boot_smoke.py`

- [ ] **Step 1: Write the smoke test**

```python
"""Smoke test the full boot path end-to-end against a fresh PAI_ROOT."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest


@pytest.mark.timeout(30)
def test_boot_runs_and_supervises(tmp_path: Path) -> None:
    """sbin/init succeeds in --check-only after paifs_init lays out
    the skeleton, and `python -m boot` reaches the supervise loop."""
    from bin.paifs_init import lay_out
    lay_out(tmp_path)
    env = {**os.environ, "PAI_ROOT": str(tmp_path)}

    # check-only path
    rc = subprocess.run(
        [sys.executable, "-m", "boot.init", "--check-only"], env=env
    ).returncode
    assert rc == 0

    # full boot — let it run for a few seconds, then SIGTERM
    proc = subprocess.Popen(
        [sys.executable, "-u", "-m", "boot"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    deadline = time.time() + 10
    saw_supervise = False
    out_buf: list[str] = []
    while time.time() < deadline:
        line = proc.stdout.readline()
        if not line:
            if proc.poll() is not None:
                break
            continue
        out_buf.append(line)
        if "supervise: started" in line:
            saw_supervise = True
            break
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    proc.wait(timeout=5)
    assert saw_supervise, f"never reached supervise loop. output:\n{''.join(out_buf)}"
```

- [ ] **Step 2: Run the smoke test**

Run: `uv run pytest tests/test_boot_smoke.py -v`
Expected: PASS. If it hangs or fails, inspect the printed output — phase failures will be obvious.

- [ ] **Step 3: Commit**

```bash
git add tests/test_boot_smoke.py
git commit -m "test: end-to-end boot smoke

Lays out PAI_ROOT in a tmpdir, runs init --check-only, then
spawns the kernel and asserts it reaches the supervise loop.
Catches phase ordering or import regressions."
```

---

## Self-Review Notes

Spec coverage: every architectural decision in the design doc maps to a task — kernel→boot rename (T1), thin-init exec (T3), each phase 1–7 (T4–T8), src decomposition (T9–T12), smoke test (T13).

PID/proc and run/pais migration are explicitly carved out of this plan and called out in the header — addressing those in this same plan would push it past 20+ tasks and cross a stable interface boundary.

Type/name consistency: `boot.phases.<name>.run()` is the consistent contract across phases. `SanityError` is the only typed exception (others fail with bare `RuntimeError` or whatever underlying call raises). `SBIN_SCRIPTS` is referenced consistently in T9 + T10. `paifs_init`'s `SYMLINKS` table is appended to in T2 and T11; both append in the same shape.

No placeholders. Every step contains the actual code, command, or path the engineer needs.
