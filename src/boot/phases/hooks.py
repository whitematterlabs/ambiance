"""Bundle lifecycle hooks — `hooks.boot:` runner.

Every installed bundle's `package.yaml` may declare:

    hooks:
      install: ["cmd", ...]   # run once after `paiman install` activates the bundle
      boot:    ["cmd", ...]   # run on every kernel boot; must be idempotent

This phase walks bundles activated under `/opt/paiman/` and runs each
bundle's `hooks.boot:` list as shell commands, cwd=$PAI_ROOT, env inherited.
Failures are logged but do not abort boot — a bad hook should not brick
the kernel.
"""
from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

import yaml

from .. import paths


def _iter_bundle_manifests() -> list[tuple[str, Path, dict]]:
    """Yield (name, bundle_dir, manifest) for every activated bundle.

    Walks `/opt/paiman/` (the canonical install location). Topic-grouped
    skill bundles live one level deeper (`/opt/paiman/<topic>/<name>/`)
    but those don't currently use boot hooks; we still surface them so
    hooks work uniformly if/when needed.
    """
    out: list[tuple[str, Path, dict]] = []
    opt = paths.opt_paiman()
    if not opt.exists():
        return out
    for entry in sorted(opt.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        pkg = entry / "package.yaml"
        if pkg.is_file():
            out.append((entry.name, entry, _load(pkg)))
            continue
        # Topic dir — recurse one level.
        for sub in sorted(entry.iterdir()):
            if not sub.is_dir() or sub.name.startswith("."):
                continue
            pkg = sub / "package.yaml"
            if pkg.is_file():
                out.append((f"{entry.name}/{sub.name}", sub, _load(pkg)))
    return out


def _load(path: Path) -> dict:
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError) as e:
        print(f"[boot] hooks: failed to read {path}: {e}", flush=True)
        return {}
    return data if isinstance(data, dict) else {}


def _hook_commands(manifest: dict, phase: str) -> list[str]:
    hooks = manifest.get("hooks") or {}
    if not isinstance(hooks, dict):
        return []
    cmds = hooks.get(phase) or []
    if isinstance(cmds, str):
        cmds = [cmds]
    return [c for c in cmds if isinstance(c, str) and c.strip()]


def _run_hook(name: str, cmd: str) -> None:
    print(f"[boot] hooks: {name}: {cmd}", flush=True)
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            cwd=str(paths.PAI_ROOT),
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        print(f"[boot] hooks: {name}: timed out after 60s — {shlex.quote(cmd)}", flush=True)
        return
    except OSError as e:
        print(f"[boot] hooks: {name}: failed to spawn — {e}", flush=True)
        return
    out = (result.stdout or "").strip()
    err = (result.stderr or "").strip()
    if out:
        for line in out.splitlines():
            print(f"[boot] hooks: {name}: {line}", flush=True)
    if result.returncode != 0:
        print(
            f"[boot] hooks: {name}: rc={result.returncode}"
            + (f" — {err}" if err else ""),
            flush=True,
        )
    elif err:
        # Some tools chatter on stderr even on success; surface it tersely.
        print(f"[boot] hooks: {name}: stderr: {err}", flush=True)


def run() -> None:
    bundles = _iter_bundle_manifests()
    if not bundles:
        return
    for name, _bundle_dir, manifest in bundles:
        for cmd in _hook_commands(manifest, "boot"):
            _run_hook(name, cmd)
