"""Kernel control plane — declarative PAI fleet config.

`etc/config.yaml` is the source of truth for which long-running PAIs exist.
The kernel reconciles `home/proc/` against the config at boot and on a
`kernel:reload_config` event.

Public API:
    load_config(path)        -> {name: resolved_spec}
    resolve_package(name)    -> dict
    reconcile_from_config()  -> None

Inert fields (v1):
    `model:` is accepted, validated, and persisted, but the runtime
    still resolves the model from `provider.yaml`. `prompt:` and
    `wake_on:` are now wired through (bootstrap.py reads per-PAI prompt
    files; main.py routes events via wake_on globs).

Reserved PIDs:
    pid 1 (`kernel_manager`) and pid 2 (`pai`) are reserved. Non-reserved
    entries omit `pid:`; the reconcile auto-allocates via
    `processes.alloc_pai_pid()` and persists into spec.yaml.

Validation runs on the *whole* config before any disk mutation, so a
broken config never half-applies.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from . import processes as P

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_PATH = REPO_ROOT / "etc" / "config.yaml"
PACKAGES_DIR = REPO_ROOT / "packages"

RESERVED_PIDS: dict[int, str] = {1: "kernel_manager", 2: "pai"}

# Fields the config is authoritative for. Reconcile rewrites these on
# spec.yaml; everything else on disk (spawned, persistent, etc.) is
# preserved across reconciles.
CONFIG_MANAGED_FIELDS = (
    "description", "prompt", "model", "wake_on", "fallback", "parent", "persistent",
)


class ConfigError(Exception):
    pass


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        raise ConfigError(f"config file not found: {path}")
    try:
        with path.open() as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise ConfigError(f"failed to parse {path}: {e}") from e
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: expected a mapping at top level")
    return data


def resolve_package(name: str) -> dict:
    """Load and validate `packages/{name}/package.yaml`. Only `kind: pai`
    is honored in v1."""
    pkg_path = PACKAGES_DIR / name / "package.yaml"
    if not pkg_path.exists():
        raise ConfigError(f"package {name!r} not found: {pkg_path}")
    data = _load_yaml(pkg_path)
    kind = data.get("kind")
    if kind != "pai":
        raise NotImplementedError(f"package kind {kind!r} not yet supported")
    return data


def _validate_pai_entry(entry: dict, *, source: str) -> None:
    name = entry.get("name")
    if not isinstance(name, str) or not name:
        raise ConfigError(f"{source}: entry missing required string `name`: {entry!r}")
    if "/" in name or name.startswith("."):
        raise ConfigError(f"{source}: invalid name {name!r}")
    if "description" not in entry or not isinstance(entry["description"], str):
        raise ConfigError(f"{source}: entry {name!r} missing string `description`")
    if "pid" in entry and not isinstance(entry["pid"], int):
        raise ConfigError(f"{source}: entry {name!r} has non-integer pid")
    if "prompt" in entry and not isinstance(entry["prompt"], str):
        raise ConfigError(f"{source}: entry {name!r} has non-string prompt")
    if "model" in entry and not isinstance(entry["model"], str):
        raise ConfigError(f"{source}: entry {name!r} has non-string model")
    if "wake_on" in entry:
        wo = entry["wake_on"]
        if not isinstance(wo, list) or not all(isinstance(p, str) for p in wo):
            raise ConfigError(f"{source}: entry {name!r} wake_on must be list[str]")
    if "fallback" in entry and not isinstance(entry["fallback"], bool):
        raise ConfigError(f"{source}: entry {name!r} fallback must be bool")
    if "parent" in entry and not isinstance(entry["parent"], int):
        raise ConfigError(f"{source}: entry {name!r} parent must be int")


def load_config(path: Path | None = None) -> dict[str, dict]:
    """Parse the config file, resolve `package:` refs, validate, return
    `{name: resolved_spec}`. Raises ConfigError on any failure (no partial
    application)."""
    if path is None:
        path = CONFIG_PATH
    raw = _load_yaml(path)
    pais = raw.get("pais") or []
    if not isinstance(pais, list):
        raise ConfigError(f"{path}: `pais` must be a list")

    resolved: dict[str, dict] = {}
    seen_pids: dict[int, str] = {}

    for entry in pais:
        if not isinstance(entry, dict):
            raise ConfigError(f"{path}: each pai entry must be a mapping, got {entry!r}")

        # Resolve package defaults first, then layer inline fields on top.
        merged: dict[str, Any] = {}
        pkg_name = entry.get("package")
        if pkg_name is not None:
            if not isinstance(pkg_name, str):
                raise ConfigError(f"{path}: `package` must be a string, got {pkg_name!r}")
            pkg = resolve_package(pkg_name)
            for k in ("description", "prompt", "model", "wake_on"):
                if k in pkg:
                    merged[k] = pkg[k]
        for k, v in entry.items():
            if k == "package":
                continue
            merged[k] = v

        _validate_pai_entry(merged, source=str(path))
        name = merged["name"]

        if name in resolved:
            raise ConfigError(f"{path}: duplicate name {name!r}")

        # Reserved-pid invariants.
        pid = merged.get("pid")
        if pid is not None:
            if pid in RESERVED_PIDS and RESERVED_PIDS[pid] != name:
                raise ConfigError(
                    f"{path}: pid {pid} is reserved for {RESERVED_PIDS[pid]!r}, "
                    f"not {name!r}"
                )
            if pid in seen_pids:
                raise ConfigError(
                    f"{path}: pid {pid} declared twice ({seen_pids[pid]!r} and {name!r})"
                )
            seen_pids[pid] = name

        # Reserved entries must declare their reserved pid.
        for reserved_pid, reserved_name in RESERVED_PIDS.items():
            if name == reserved_name and pid != reserved_pid:
                raise ConfigError(
                    f"{path}: reserved entry {name!r} must declare pid {reserved_pid}"
                )

        resolved[name] = merged

    return resolved


def _spec_diff(desired: dict, actual: dict) -> list[str]:
    """Return list of CONFIG_MANAGED_FIELDS that differ."""
    changed: list[str] = []
    for k in CONFIG_MANAGED_FIELDS:
        if desired.get(k) != actual.get(k):
            changed.append(k)
    return changed


def _apply_managed_fields(target: dict, desired: dict) -> None:
    """Overwrite config-managed fields on `target` from `desired`. Removes
    keys from target if absent in desired (so dropping `wake_on` from config
    clears it on disk)."""
    for k in CONFIG_MANAGED_FIELDS:
        if k in desired:
            target[k] = desired[k]
        elif k in target:
            del target[k]


def reconcile_from_config(path: Path | None = None) -> None:
    """Diff the desired fleet (from config) against `home/proc/` and apply.

    Adds:    spawn the new PAI, log it.
    Removes: resolve cancelled, leave proc dir on disk.
    Changes: rewrite spec.yaml in place. PID is invariant — never changes
             for an existing name.
    """
    desired = load_config(path)
    # Every config-declared PAI is, by definition, persistent — long-running
    # fleet members the kernel keeps alive across nudges. The flag drives
    # nudge.py's "don't auto-resolve on completion" behavior.
    for spec in desired.values():
        spec["persistent"] = True
    actual = {slug: spec for slug, spec in P._iter_pai_specs()}

    desired_names = set(desired)
    actual_names = set(actual)

    # Pid invariant: catch this before any disk mutation.
    for name in desired_names & actual_names:
        d_pid = desired[name].get("pid")
        a_pid = actual[name].get("pid")
        if d_pid is not None and a_pid is not None and d_pid != a_pid:
            raise ConfigError(
                f"pid for existing PAI {name!r} cannot change "
                f"(disk: {a_pid}, config: {d_pid})"
            )

    # Added.
    for name in sorted(desired_names - actual_names):
        spec = desired[name]
        pid = spec.get("pid")
        if pid is None:
            pid = P.alloc_pai_pid()
        P.spawn_pai(
            pid=pid,
            slug=name,
            description=spec["description"],
            prompt=spec.get("prompt"),
            model=spec.get("model"),
            wake_on=spec.get("wake_on"),
            fallback=spec.get("fallback"),
            parent=spec.get("parent"),
        )
        try:
            P.append_log(name, "kernel: spawned via reconcile")
        except P.ProcessNotFound:
            pass
        print(f"[kernel] reconcile: spawned pai {name!r} (pid={pid})", flush=True)

    # Removed.
    for name in sorted(actual_names - desired_names):
        # Only remove cleanly-managed PAIs (skip ones already cancelled to
        # avoid log churn). We treat any non-running status as already-removed.
        try:
            status = P.read_status(name)
        except P.ProcessNotFound:
            continue
        if status != "running":
            continue
        try:
            P.resolve(name, "cancelled")
            print(f"[kernel] reconcile: cancelled pai {name!r}", flush=True)
        except P.ProcessNotFound:
            pass

    # Changed.
    for name in sorted(desired_names & actual_names):
        diff = _spec_diff(desired[name], actual[name])
        if diff:
            spec_path = P.PROC_DIR / name / "spec.yaml"
            on_disk = dict(actual[name])
            _apply_managed_fields(on_disk, desired[name])
            with spec_path.open("w") as f:
                yaml.safe_dump(on_disk, f, sort_keys=False)
            try:
                P.append_log(name, f"kernel: spec updated via reconcile ({', '.join(diff)})")
            except P.ProcessNotFound:
                pass
            print(f"[kernel] reconcile: updated pai {name!r} ({', '.join(diff)})", flush=True)

        # Status heal: persistent PAIs are invariantly running. If anything
        # left them resolved (legacy bug, manual edit, crash), restore.
        try:
            status = P.read_status(name)
        except P.ProcessNotFound:
            continue
        if status != "running":
            (P.PROC_DIR / name / "status").write_text("running\n")
            try:
                P.append_log(name, f"kernel: status healed ({status} → running)")
            except P.ProcessNotFound:
                pass
            print(
                f"[kernel] reconcile: healed status for pai {name!r} "
                f"({status} → running)",
                flush=True,
            )
