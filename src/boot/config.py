"""Kernel control plane — declarative PAI fleet config.

`etc/config.yaml` is the source of truth for which long-running PAIs exist
and how they are wired (provider, model, prompt, wake routing). The kernel
reconciles `home/proc/` against the config at boot and on a
`kernel:reload_config` event.

Public API:
    load_config(path)        -> {name: resolved_spec}
    resolve_package(name)    -> dict
    reconcile_from_config()  -> None

Reserved PIDs:
    pid 1 (`root`) and pid 2 (`pai`) are reserved. Non-reserved
    entries omit `pid:`; the reconcile auto-allocates via
    `processes.alloc_pai_pid()` and persists into spec.yaml.

Validation runs on the *whole* config before any disk mutation, so a
broken config never half-applies.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from . import llm as L
from . import processes as P
from . import paths

CONFIG_PATH = paths.etc() / "config.yaml"
PACKAGES_DIR = paths.usr_lib_pais()
SUBAGENTS_DIR = paths.usr_lib_subagents()

RESERVED_PIDS: dict[int, str] = {1: "root", 2: "pai"}

# Fields the config is authoritative for. Reconcile rewrites these on
# spec.yaml; everything else on disk (spawned, persistent, etc.) is
# preserved across reconciles.
CONFIG_MANAGED_FIELDS = (
    "description", "prompt", "provider", "model", "wake_on",
    "fallback", "parent", "persistent", "active", "dependencies",
)

# Fields a `dependencies:` entry can carry (each entry materializes a persub
# child of the declaring PAI). `name` is required; everything else inherits
# from the parent or has a sensible default.
DEP_FIELDS = ("name", "description", "prompt", "provider", "model", "package")


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


def resolve_subagent_package(name: str) -> dict:
    """Load and validate a subagent bundle from `/usr/lib/subagents/{name}/`.
    Used by `dependencies:` entries that say `package: <name>` to pull
    prompt/provider/model defaults from a shared bundle."""
    pkg_path = SUBAGENTS_DIR / name / "package.yaml"
    if not pkg_path.exists():
        raise ConfigError(f"subagent package {name!r} not found: {pkg_path}")
    data = _load_yaml(pkg_path)
    kind = data.get("kind")
    if kind != "subagent":
        raise ConfigError(
            f"subagent package {name!r}: expected kind=subagent, got {kind!r}"
        )
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
    if "provider" in entry:
        prov = entry["provider"]
        if not isinstance(prov, str):
            raise ConfigError(f"{source}: entry {name!r} has non-string provider")
        if prov not in L.PROVIDERS:
            known = ", ".join(sorted(L.PROVIDERS))
            raise ConfigError(
                f"{source}: entry {name!r} unknown provider {prov!r} "
                f"(known: {known})"
            )
    if "model" in entry and not isinstance(entry["model"], str):
        raise ConfigError(f"{source}: entry {name!r} has non-string model")
    if "wake_on" in entry:
        wo = entry["wake_on"]
        if not isinstance(wo, list) or not all(isinstance(p, str) for p in wo):
            raise ConfigError(f"{source}: entry {name!r} wake_on must be list[str]")
    if "fallback" in entry and not isinstance(entry["fallback"], bool):
        raise ConfigError(f"{source}: entry {name!r} fallback must be bool")
    if "active" in entry and not isinstance(entry["active"], bool):
        raise ConfigError(f"{source}: entry {name!r} active must be bool")
    if "parent" in entry and not isinstance(entry["parent"], int):
        raise ConfigError(f"{source}: entry {name!r} parent must be int")
    if "dependencies" in entry:
        deps = entry["dependencies"]
        if not isinstance(deps, list):
            raise ConfigError(f"{source}: entry {name!r} dependencies must be a list")
        seen: set[str] = set()
        for dep in deps:
            if not isinstance(dep, dict):
                raise ConfigError(
                    f"{source}: entry {name!r} dependencies items must be mappings "
                    f"(bare-name shorthand not yet supported); got {dep!r}"
                )
            dep_name = dep.get("name")
            if not isinstance(dep_name, str) or not dep_name:
                raise ConfigError(
                    f"{source}: entry {name!r} dependency missing string `name`: {dep!r}"
                )
            if "/" in dep_name or "." in dep_name or dep_name.startswith("-"):
                raise ConfigError(
                    f"{source}: entry {name!r} invalid dependency name {dep_name!r}"
                )
            if dep_name in seen:
                raise ConfigError(
                    f"{source}: entry {name!r} duplicate dependency {dep_name!r}"
                )
            seen.add(dep_name)
            if "description" not in dep or not isinstance(dep["description"], str):
                raise ConfigError(
                    f"{source}: entry {name!r} dependency {dep_name!r} missing string `description`"
                )
            for k in ("prompt", "provider", "model", "package"):
                if k in dep and not isinstance(dep[k], str):
                    raise ConfigError(
                        f"{source}: entry {name!r} dependency {dep_name!r} field {k!r} must be a string"
                    )
            if "provider" in dep and dep["provider"] not in L.PROVIDERS:
                known = ", ".join(sorted(L.PROVIDERS))
                raise ConfigError(
                    f"{source}: entry {name!r} dependency {dep_name!r} unknown provider "
                    f"{dep['provider']!r} (known: {known})"
                )
            for k in dep:
                if k not in DEP_FIELDS:
                    raise ConfigError(
                        f"{source}: entry {name!r} dependency {dep_name!r} unknown field {k!r}"
                    )
            if "package" in dep:
                # Fail fast at load time so missing/malformed bundles
                # never half-spawn a persub.
                try:
                    resolve_subagent_package(dep["package"])
                except ConfigError as e:
                    raise ConfigError(
                        f"{source}: entry {name!r} dependency {dep_name!r} {e}"
                    )


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
            for k in ("description", "prompt", "provider", "model", "wake_on"):
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
    # `active` defaults to True when omitted; paictl flips it to take a PAI
    # down without removing the fleet entry.
    for spec in desired.values():
        spec["persistent"] = True
        spec.setdefault("active", True)
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
        if not spec.get("active", True):
            # Inactive at first sight: don't materialize a /proc entry. When
            # paictl flips `active: true`, the next reconcile spawns it.
            print(f"[kernel] reconcile: skipping inactive pai {name!r}", flush=True)
            continue
        pid = spec.get("pid")
        if pid is None:
            pid = P.alloc_pai_pid()
        P.spawn_pai(
            pid=pid,
            slug=name,
            description=spec["description"],
            prompt=spec.get("prompt"),
            provider=spec.get("provider"),
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
        # Persubs and ad-hoc subagents are owned by their parent, not the
        # top-level fleet config — skip them so reconcile doesn't cancel
        # children just because they're absent from /etc/config.yaml.
        if "parent" in actual[name]:
            continue
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

        # Status reconcile.
        # - active PAIs are invariantly running; heal anything else back.
        # - inactive PAIs are invariantly stopped; resolve a running proc.
        try:
            status = P.read_status(name)
        except P.ProcessNotFound:
            continue
        active = desired[name].get("active", True)
        if active and status != "running":
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
        elif not active and status == "running":
            try:
                P.resolve(name, "stopped")
                P.append_log(name, "kernel: stopped via active=false")
            except P.ProcessNotFound:
                pass
            print(f"[kernel] reconcile: stopped inactive pai {name!r}", flush=True)

    _reconcile_persubs(desired)


def _reconcile_persubs(desired: dict[str, dict]) -> None:
    """For each parent declaring `dependencies:`, spawn each persub child
    if its /proc dir does not yet exist. Idempotent — does not update or
    teardown existing persubs in this pass (see plan for out-of-scope items)."""
    for parent_name, parent_spec in sorted(desired.items()):
        deps = parent_spec.get("dependencies") or []
        if not deps:
            continue
        if not parent_spec.get("active", True):
            continue
        parent_pid = parent_spec.get("pid")
        if parent_pid is None:
            # Parent didn't have an explicit pid in config; read from disk.
            parent_pid = P.read_pai_pid(parent_name)
        if parent_pid is None:
            print(
                f"[kernel] reconcile: cannot spawn persubs for {parent_name!r} — no pid",
                flush=True,
            )
            continue
        for dep in deps:
            dep_name = dep["name"]
            slug = f"{parent_name}.{dep_name}"
            if (P.PROC_DIR / slug).exists():
                # Persub already exists; heal its status if shutdown left it
                # at "stopped" (or anything else). Fleet members get the same
                # treatment in the reconcile_from_config "Changed" branch.
                try:
                    status = P.read_status(slug)
                except P.ProcessNotFound:
                    continue
                if status != "running":
                    (P.PROC_DIR / slug / "status").write_text("running\n")
                    try:
                        P.append_log(slug, f"kernel: status healed ({status} → running)")
                    except P.ProcessNotFound:
                        pass
                    print(
                        f"[kernel] reconcile: healed persub {slug!r} ({status} → running)",
                        flush=True,
                    )
                continue
            # Resolution chain (highest wins): dep override → bundle → parent.
            bundle: dict = {}
            if dep.get("package"):
                bundle = resolve_subagent_package(dep["package"])
            prompt = dep.get("prompt") or bundle.get("prompt")
            provider = (
                dep.get("provider")
                or bundle.get("provider")
                or parent_spec.get("provider")
            )
            model = (
                dep.get("model")
                or bundle.get("model")
                or parent_spec.get("model")
            )
            child_pid = P.alloc_pai_pid()
            P.spawn_pai(
                pid=child_pid,
                slug=slug,
                description=dep["description"],
                prompt=prompt,
                provider=provider,
                model=model,
                parent=parent_pid,
                extra={"persistent": True, "persub": True},
            )
            try:
                P.append_log(slug, "kernel: spawned persub via reconcile")
            except P.ProcessNotFound:
                pass
            print(
                f"[kernel] reconcile: spawned persub {slug!r} (pid={child_pid}, parent={parent_pid})",
                flush=True,
            )
