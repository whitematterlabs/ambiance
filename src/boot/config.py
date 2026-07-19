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
from . import timers as T

CONFIG_PATH = paths.etc() / "config.yaml"
PACKAGES_DIR = paths.usr_lib_pais()
SUBAGENTS_DIR = paths.usr_lib_subagents()

RESERVED_PIDS: dict[int, str] = {1: "root", 2: "pai"}

# Fields the config is authoritative for. Reconcile rewrites these on
# spec.yaml; everything else on disk (spawned, persistent, etc.) is
# preserved across reconciles.
CONFIG_MANAGED_FIELDS = (
    "description", "display_name", "prompt", "prompt_dir", "boilerplate",
    "provider", "model", "backend", "wake_on", "fallback", "parent",
    "persistent", "active", "compact_threshold", "hard_compact_threshold",
    "heartbeat",
)

# Turn-executor backends. Default (omitted/None) is the in-process Anthropic
# loop in boot.llm. `claudecode` runs the turn through the `claude` CLI inside
# the PAI's FHS home (see boot.claude_backend).
KNOWN_BACKENDS = ("claudecode",)

# Owner-granted send capabilities. Declared at the TOP LEVEL of config.yaml
# (`capabilities:`, a sibling of `pais:`) because they are PAI-agnostic — they
# describe what the whole system may do on the owner's behalf, not how one
# fleet member is wired. Each flag does double duty: the kernel projects it
# into the matching driver's freeze file (enforcement) AND bootstrap renders
# it into the PAI's `<capabilities>` prompt block (honesty), both from this one
# source, so what a PAI is told and what the driver allows can never drift.
#
# `driver`/`freeze` locate the freeze file under sys/drivers/<driver>/<freeze>;
# its presence means "outbound frozen". `mounts` is the set of mounted-driver
# names for which the flag is relevant (drives whether the prompt block
# mentions it). Default for every flag is DENY (frozen) when absent.
CAPABILITY_SPECS: dict[str, dict] = {
    "email_send": {
        "driver": "email", "freeze": "outbound.freeze", "mounts": {"email"},
    },
    "imessage_send": {
        "driver": "imessage", "freeze": "outbound.freeze", "mounts": {"imessage"},
    },
    "whatsapp_send": {
        "driver": "whatsapp", "freeze": "outbound.freeze", "mounts": {"whatsapp"},
    },
    "slack_send": {
        "driver": "slack", "freeze": "outbound.freeze", "mounts": {"slack"},
    },
    # Ambient-capture gates, not send freezes: the freeze file gates whether
    # the driver captures at all (presence = capture disabled). `default` is
    # the mode when the key is absent from config.yaml — send flags stay
    # fail-closed, cowork ships on-by-default per its spec. `modes` restricts
    # the tri-state: "ask" is meaningless for capture (a capture either
    # happens or it doesn't), so these are two-state and an out-of-range mode
    # clamps to `no`.
    #
    # Cowork is three independent facets, one flag + freeze file each, so the
    # owner can leave file activity on while killing clipboard capture.
    # `legacy` names the pre-split single key: a config that still says
    # `cowork: no` keeps meaning "all three off" until a facet key overrides.
    "cowork_window": {
        "driver": "cowork", "freeze": "window.freeze", "mounts": {"cowork"},
        "default": "yes", "modes": ("no", "yes"), "legacy": "cowork",
    },
    "cowork_clipboard": {
        "driver": "cowork", "freeze": "clipboard.freeze", "mounts": {"cowork"},
        "default": "yes", "modes": ("no", "yes"), "legacy": "cowork",
    },
    "cowork_files": {
        "driver": "cowork", "freeze": "files.freeze", "mounts": {"cowork"},
        "default": "yes", "modes": ("no", "yes"), "legacy": "cowork",
    },
    "notetaker": {
        "driver": "notetaker", "freeze": "capture.freeze", "mounts": {"notetaker"},
        "default": "no", "modes": ("no", "yes"),
    },
    # Calendar write is a *bin* gate, not a driver-send gate: there is no
    # outbound driver to freeze, so the `write_calendar` bin reads this mode
    # live from `config.capability_modes()` and refuses unless it is `yes`.
    # The freeze file is still projected onto the calendar driver's state dir
    # as the visible marker (and for parity with the other flags), but the bin
    # is the enforcement point. Two-state (no/yes), fail-closed — there is no
    # approvals hand-off for a direct EventKit write, so "ask" is meaningless.
    "calendar_write": {
        "driver": "calendar", "freeze": "write.freeze", "mounts": {"calendar"},
        "default": "no", "modes": ("no", "yes"),
    },
    # Computer use is an *actuation* gate, not a send-driver freeze. The `ax`
    # accessibility sidecar can drive any Mac app's GUI — click, type, press
    # Send — which is a full bypass of the per-channel send freezes above (it
    # reaches the app's own Send button instead of going through the frozen
    # outbound driver). So `axd` reads this freeze on every actuation and
    # refuses `act` unless the mode is `yes`. Two-state (no/yes), fail-closed,
    # default OFF: GUI actuation is synchronous with no approvals hand-off, so
    # "ask" is meaningless (same reasoning as calendar_write/cowork). Enforced
    # inside the sidecar — a process the PAI does not control — not in the `ax`
    # client, which the PAI could bypass by speaking to the socket directly.
    # Independently, `axd` also honors the *_send freezes above for the app it
    # is attached to (Messages→imessage_send, Mail→email_send, …), so a granted
    # computer_use still cannot press Send in a channel whose sends are frozen.
    "computer_use": {
        "driver": "ax", "freeze": "control.freeze", "mounts": {"ax"},
        "default": "no", "modes": ("no", "yes"),
    },
    # Shell execution is a *kernel* gate, not a driver freeze: `driver: None`
    # means no freeze file is projected and the flag is relevant to every PAI
    # regardless of mounted drivers (every PAI has the bash/shell tools).
    # Enforcement lives in boot.bash_gate at the tool-dispatch boundary. In
    # `ask` mode, commands matching the owner's `bash_allowlist:` prefix
    # rules run directly; everything else blocks on the approval tray.
    # Default `yes` — existing installs keep their behavior until the owner
    # flips the sidebar toggle.
    "bash_exec": {
        "driver": None, "freeze": None, "mounts": None,
        "default": "yes",
    },
}

def _boilerplate_dir(config_path: Path) -> Path:
    """Boilerplate lives next to the config file (etc/boilerplate/), so that
    tests pointing at a synthetic etc/ get their own boilerplate scope."""
    return config_path.parent / "boilerplate"


def _validate_boilerplate_names(
    names: list, *, where: str, config_path: Path
) -> None:
    if not isinstance(names, list) or not all(isinstance(n, str) and n for n in names):
        raise ConfigError(f"{where} boilerplate must be list[str]")
    base = _boilerplate_dir(config_path)
    for n in names:
        if not (base / f"{n}.md").exists():
            raise ConfigError(
                f"{where} boilerplate {n!r} not found at {base / f'{n}.md'}"
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


def resolve_subagent_package(name: str) -> dict:
    """Load and validate a subagent bundle from `/usr/lib/subagents/{name}/`.
    Used by `subagent spawn --package <name>` to pull prompt/provider/model
    defaults from a shared bundle."""
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


def _resolve_subagent_bundle_path(package: str, value: str) -> str:
    """Resolve a path coming from a subagent bundle manifest.

    Registry bundles commonly use FHS-relative paths like
    `usr/lib/subagents/computer-use/prompt.md`, which bootstrap already resolves
    against PAI_ROOT. Short paths like `prompt.md` are bundle-local and need
    expansion to the installed bundle directory.
    """
    p = Path(value)
    if p.is_absolute():
        return value
    if value.startswith(("usr/", "opt/", "etc/")):
        return value
    return str(SUBAGENTS_DIR / package / value)


def _validate_pai_entry(entry: dict, *, source: str, config_path: Path) -> None:
    name = entry.get("name")
    if not isinstance(name, str) or not name:
        raise ConfigError(f"{source}: entry missing required string `name`: {entry!r}")
    # ":" is reserved for synthetic timer slugs (timers.HEARTBEAT_PREFIX), so
    # no real PAI name may contain it — same guard paiclone applies.
    if "/" in name or ":" in name or name.startswith("."):
        raise ConfigError(f"{source}: invalid name {name!r}")
    if "description" not in entry or not isinstance(entry["description"], str):
        raise ConfigError(f"{source}: entry {name!r} missing string `description`")
    if "display_name" in entry and not isinstance(entry["display_name"], str):
        raise ConfigError(f"{source}: entry {name!r} has non-string display_name")
    if "pid" in entry and not isinstance(entry["pid"], int):
        raise ConfigError(f"{source}: entry {name!r} has non-integer pid")
    if "prompt" in entry and not isinstance(entry["prompt"], str):
        raise ConfigError(f"{source}: entry {name!r} has non-string prompt")
    if "prompt_dir" in entry and not isinstance(entry["prompt_dir"], str):
        raise ConfigError(f"{source}: entry {name!r} has non-string prompt_dir")
    if "boilerplate" in entry:
        _validate_boilerplate_names(
            entry["boilerplate"],
            where=f"{source}: entry {name!r}",
            config_path=config_path,
        )
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
    if "backend" in entry:
        be = entry["backend"]
        if be is not None and be not in KNOWN_BACKENDS:
            known = ", ".join(KNOWN_BACKENDS)
            raise ConfigError(
                f"{source}: entry {name!r} unknown backend {be!r} (known: {known})"
            )
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
    if "compact_threshold" in entry:
        ct = entry["compact_threshold"]
        if not isinstance(ct, int) or isinstance(ct, bool) or ct <= 0:
            raise ConfigError(
                f"{source}: entry {name!r} compact_threshold must be a positive int"
            )
    if "hard_compact_threshold" in entry:
        hct = entry["hard_compact_threshold"]
        if not isinstance(hct, int) or isinstance(hct, bool) or hct <= 0:
            raise ConfigError(
                f"{source}: entry {name!r} hard_compact_threshold must be a positive int"
            )
        # The hardline backstop must sit above the soft threshold, else it
        # would pre-empt the cooperative compaction it's meant to back up.
        soft = entry.get("compact_threshold")
        if isinstance(soft, int) and not isinstance(soft, bool) and hct <= soft:
            raise ConfigError(
                f"{source}: entry {name!r} hard_compact_threshold ({hct}) must be "
                f"greater than compact_threshold ({soft})"
            )
    if "heartbeat" in entry and entry["heartbeat"] is not None:
        hb = entry["heartbeat"]
        try:
            secs = T.parse_duration(hb)
        except ValueError as e:
            raise ConfigError(f"{source}: entry {name!r} heartbeat: {e}") from None
        # Sub-minute beats are an LLM-spend footgun, not a scheduling need.
        if secs < 60:
            raise ConfigError(
                f"{source}: entry {name!r} heartbeat must be at least 60s, "
                f"got {hb!r}"
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
            for k in (
                "description", "prompt_dir", "boilerplate",
                "provider", "model", "wake_on",
            ):
                if k in pkg:
                    merged[k] = pkg[k]
            # Default prompt_dir for a packaged PAI is the bundle dir
            # itself — every `*.md` inside it is the PAI's custom prose.
            merged.setdefault("prompt_dir", str(PACKAGES_DIR / pkg_name))
            # Bundle paths are relative to the bundle dir; rewrite to
            # absolute so bootstrap can read without knowing about packages.
            if not Path(merged["prompt_dir"]).is_absolute():
                merged["prompt_dir"] = str(PACKAGES_DIR / pkg_name / merged["prompt_dir"])
        for k, v in entry.items():
            if k == "package":
                continue
            merged[k] = v

        _validate_pai_entry(merged, source=str(path), config_path=path)
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


def is_fallback(slug: str, path: Path | None = None) -> bool:
    """Return True iff `slug` has `fallback: true` in /etc/config.yaml.
    Read-only and tolerant: missing config or malformed entry → False."""
    if path is None:
        path = CONFIG_PATH
    if not path.exists():
        return False
    try:
        raw = _load_yaml(path)
    except ConfigError:
        return False
    for entry in raw.get("pais") or []:
        if isinstance(entry, dict) and entry.get("name") == slug:
            return bool(entry.get("fallback"))
    return False


def onboarding_pending(path: Path | None = None) -> bool:
    """Return True iff the top-level `onboarding_pending` key is truthy in
    /etc/config.yaml. Read-only and tolerant: missing/malformed config →
    False. The flag is a top-level key (not under `pais`), so it is inert to
    `load_config`/`reconcile_from_config` and never leaks into a spec.yaml."""
    if path is None:
        path = CONFIG_PATH
    if not path.exists():
        return False
    try:
        raw = _load_yaml(path)
    except ConfigError:
        return False
    return bool(raw.get("onboarding_pending"))


def clear_onboarding_pending(path: Path | None = None) -> None:
    """Set the top-level `onboarding_pending` key to False, preserving the
    rest of the config (round-trips the whole dict). Atomic: .tmp + rename,
    mirroring `paiadd._append_entry`. Tolerant of a missing/malformed file —
    nothing to clear, so it's a no-op."""
    if path is None:
        path = CONFIG_PATH
    if not path.exists():
        return
    try:
        raw = _load_yaml(path)
    except ConfigError:
        return
    raw["onboarding_pending"] = False
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w") as f:
        yaml.safe_dump(raw, f, sort_keys=False)
    tmp.rename(path)


def package_for(slug: str, path: Path | None = None) -> str | None:
    """Return the bundle name declared for `slug` in /etc/config.yaml, or
    None if the slug is bundleless (or absent). Read-only — does not
    validate or resolve the bundle."""
    if path is None:
        path = CONFIG_PATH
    if not path.exists():
        return None
    raw = _load_yaml(path)
    for entry in raw.get("pais") or []:
        if isinstance(entry, dict) and entry.get("name") == slug:
            pkg = entry.get("package")
            return pkg if isinstance(pkg, str) else None
    return None


def clone_of(slug: str, path: Path | None = None) -> str | None:
    """Return the source name `slug` was cloned from, or None if it is an
    original (or absent). Read-only and tolerant: missing/malformed config → None.

    `clone_of` is a behavior-free provenance marker stamped by paiclone; it is
    not a CONFIG_MANAGED_FIELD and never reaches /proc/<slug>/spec.yaml, so this
    reads the config directly — the only source of truth for the marker."""
    if path is None:
        path = CONFIG_PATH
    if not path.exists():
        return None
    try:
        raw = _load_yaml(path)
    except ConfigError:
        return None
    for entry in raw.get("pais") or []:
        if isinstance(entry, dict) and entry.get("name") == slug:
            src = entry.get("clone_of")
            return src if isinstance(src, str) and src else None
    return None


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


# A capability is tri-state. Legacy bools still parse (true→yes, false→no):
#   no   — channel cannot send; the PAI drafts only.
#   ask  — the PAI may *propose* a send; it lands in the owner's approval
#          queue and is delivered only after the owner approves it. Outbound
#          stays frozen for the PAI's own direct sends — only the approvals
#          driver carries an approved item through. The default-safe grant.
#   yes  — the PAI sends autonomously at its own discretion.
# Default is `no` (fail-closed): a missing/broken/typo'd value never sends.
CAPABILITY_MODES = ("no", "ask", "yes")


def _normalize_capability_mode(value) -> str:
    """Map a raw `capabilities:` value to one of CAPABILITY_MODES, fail-closed.

    Accepts legacy bools (true→yes, false→no) and string aliases; anything
    unrecognized — a typo, a number, a list — resolves to `no` so a malformed
    grant can never enable sending."""
    if value is True:
        return "yes"
    if value is False or value is None:
        return "no"
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("yes", "on", "auto", "true"):
            return "yes"
        if v in ("ask", "approve", "approval", "review"):
            return "ask"
        if v in ("no", "off", "false", "none", "drafts", "draft", ""):
            return "no"
    return "no"


def capability_modes(path: Path | None = None) -> dict[str, str]:
    """Read the top-level `capabilities:` map as a mode (no/ask/yes) per
    capability. A missing key resolves to the flag's spec `default` ("no"
    unless the spec says otherwise — cowork is the deliberate default-yes
    exception); a missing file, parse error, or unrecognized value resolves
    to `no` — a grant must come from a well-formed, readable config, never
    from a broken or typo'd one. A mode outside the flag's allowed `modes`
    (e.g. "ask" on a two-state capture gate) clamps to `no`.

    A flag whose key is absent falls back to its spec `legacy` key when the
    config still carries one (the pre-split `cowork:` toggle seeds all three
    facets), and only then to the spec default."""
    p = path or CONFIG_PATH
    try:
        data = _load_yaml(p)
    except ConfigError:
        return {k: "no" for k in CAPABILITY_SPECS}
    caps = data.get("capabilities") if isinstance(data, dict) else None
    caps = caps if isinstance(caps, dict) else {}
    out: dict[str, str] = {}
    for k, spec in CAPABILITY_SPECS.items():
        legacy = spec.get("legacy")
        if k in caps:
            mode = _normalize_capability_mode(caps.get(k))
        elif legacy is not None and legacy in caps:
            mode = _normalize_capability_mode(caps.get(legacy))
        else:
            mode = spec.get("default", "no")
        if mode not in spec.get("modes", CAPABILITY_MODES):
            mode = "no"
        out[k] = mode
    return out


def set_capability_mode(flag: str, mode: str, path: Path | None = None) -> str:
    """Write `capabilities.<flag> = <mode>` into config.yaml and return the mode.

    Strict, unlike the tolerant read path: an unknown `flag` or a `mode` outside
    `CAPABILITY_MODES` raises ValueError rather than silently coercing to `off`.
    Reading fails *closed* (a typo never sends); writing fails *loud* (a typo is a
    caller bug we surface, not persist). Preserves every other key in the file —
    a full-document round-trip via the same yaml path paiadd/paidel use, so the
    hand-maintained fleet block survives. Atomic (tmp + rename). The caller emits
    `kernel:reload_config` so `project_capabilities` re-projects the freeze."""
    if flag not in CAPABILITY_SPECS:
        raise ValueError(f"unknown send capability: {flag!r}")
    allowed = CAPABILITY_SPECS[flag].get("modes", CAPABILITY_MODES)
    if mode not in allowed:
        raise ValueError(f"capability {flag!r} accepts {allowed}, got {mode!r}")
    p = path or CONFIG_PATH
    data = _load_yaml(p) if p.exists() else {}
    caps = data.get("capabilities")
    if not isinstance(caps, dict):
        caps = {}
    caps[flag] = mode
    data["capabilities"] = caps
    tmp = p.with_suffix(p.suffix + ".tmp")
    with tmp.open("w") as f:
        yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True)
    tmp.rename(p)
    return mode


def bash_allowlist(path: Path | None = None) -> list[str]:
    """Owner prefix rules the bash gate consults in `ask` mode — top-level
    `bash_allowlist:` list in config.yaml. Tolerant like capability_modes:
    a missing/broken file or malformed list reads as empty (nothing
    auto-allowed), never an error."""
    p = path or CONFIG_PATH
    try:
        data = _load_yaml(p)
    except ConfigError:
        return []
    rules = data.get("bash_allowlist") if isinstance(data, dict) else None
    if not isinstance(rules, list):
        return []
    return [r.strip() for r in rules if isinstance(r, str) and r.strip()]


def set_bash_allowlist(rules: list[str], path: Path | None = None) -> list[str]:
    """Write the full `bash_allowlist:` list and return it (deduped, order
    kept). Strict like set_capability_mode: a non-string or blank rule
    raises. An empty list removes the key. Atomic (tmp + rename)."""
    clean: list[str] = []
    for r in rules:
        if not isinstance(r, str) or not r.strip():
            raise ValueError(f"allowlist rule must be a non-empty string: {r!r}")
        r = r.strip()
        if r not in clean:
            clean.append(r)
    p = path or CONFIG_PATH
    data = _load_yaml(p) if p.exists() else {}
    if clean:
        data["bash_allowlist"] = clean
    else:
        data.pop("bash_allowlist", None)
    tmp = p.with_suffix(p.suffix + ".tmp")
    with tmp.open("w") as f:
        yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True)
    tmp.rename(p)
    return clean


# Channels that can carry a recipient allowlist (`send_allowlist:` keys).
SEND_ALLOWLIST_CHANNELS = ("imessage", "whatsapp", "email")


def send_allowlist(channel: str, path: Path | None = None) -> list[str]:
    """Owner recipient rules a send driver consults in `ask` mode —
    top-level `send_allowlist:` map in config.yaml, keyed by channel.
    Tolerant like bash_allowlist: missing/broken file, malformed map, or
    unknown channel reads as empty (nothing auto-allowed), never an error."""
    if channel not in SEND_ALLOWLIST_CHANNELS:
        return []
    p = path or CONFIG_PATH
    try:
        data = _load_yaml(p)
    except ConfigError:
        return []
    table = data.get("send_allowlist") if isinstance(data, dict) else None
    rules = table.get(channel) if isinstance(table, dict) else None
    if not isinstance(rules, list):
        return []
    return [r.strip() for r in rules if isinstance(r, str) and r.strip()]


def set_send_allowlist(
    channel: str, rules: list[str], path: Path | None = None
) -> list[str]:
    """Write one channel's `send_allowlist:` list and return it (deduped,
    order kept). Strict like set_bash_allowlist: unknown channel or a
    non-string/blank rule raises. An empty list removes the channel key;
    an empty map removes `send_allowlist:` entirely. Atomic (tmp + rename)."""
    if channel not in SEND_ALLOWLIST_CHANNELS:
        raise ValueError(f"unknown send_allowlist channel: {channel!r}")
    clean: list[str] = []
    for r in rules:
        if not isinstance(r, str) or not r.strip():
            raise ValueError(f"allowlist rule must be a non-empty string: {r!r}")
        r = r.strip()
        if r not in clean:
            clean.append(r)
    p = path or CONFIG_PATH
    data = _load_yaml(p) if p.exists() else {}
    table = data.get("send_allowlist")
    if not isinstance(table, dict):
        table = {}
    if clean:
        table[channel] = clean
    else:
        table.pop(channel, None)
    if table:
        data["send_allowlist"] = table
    else:
        data.pop("send_allowlist", None)
    tmp = p.with_suffix(p.suffix + ".tmp")
    with tmp.open("w") as f:
        yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True)
    tmp.rename(p)
    return clean


def set_pai_model(name: str, provider: str, model: str, path: Path | None = None) -> dict[str, str]:
    """Write `provider:`/`model:` on one fleet entry and return them.

    Strict like set_capability_mode: an unknown provider or absent PAI raises
    ValueError (the web surface maps it to a 400). Full-document round-trip via
    the same yaml path paiadd uses — comments don't survive, which is the trade
    the fleet block already lives with. Atomic (tmp + rename); on any failure
    the file is untouched. The caller emits `kernel:reload_config`.
    """
    # The picker addresses a turn-executor backend (boot.KNOWN_BACKENDS) with
    # the same provider slot — e.g. `claudecode`. Such a pick writes `backend:`
    # (and drops `provider:`) instead of validating against L.PROVIDERS.
    backend = provider if provider in KNOWN_BACKENDS else None
    if backend is None and provider not in L.PROVIDERS:
        known = ", ".join(sorted(L.PROVIDERS))
        raise ValueError(f"unknown provider {provider!r} (known: {known})")
    model = model.strip()
    if not model:
        raise ValueError("model must be non-empty")
    p = path or CONFIG_PATH
    data = _load_yaml(p) if p.exists() else {}
    pais = data.get("pais") if isinstance(data, dict) else None
    entry = None
    if isinstance(pais, list):
        entry = next(
            (e for e in pais if isinstance(e, dict) and e.get("name") == name), None
        )
    if entry is None:
        raise ValueError(f"unknown pai: {name!r}")
    if backend:
        entry["backend"] = backend
        entry.pop("provider", None)  # a backend owns its own routing
    else:
        entry["provider"] = provider
        entry.pop("backend", None)  # switching back to a provider clears it
    entry["model"] = model
    tmp = p.with_suffix(p.suffix + ".tmp")
    with tmp.open("w") as f:
        yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True)
    tmp.rename(p)
    return {"name": name, "provider": provider, "model": model, "backend": backend}


def set_pai_display_name(
    name: str, display_name: str, path: Path | None = None
) -> dict[str, str]:
    """Write `display_name:` on one fleet entry (the owner-facing rename).

    The slug (`name`) is the stable identity — homes, proc dirs, routing all
    hang off it — so a rename only ever touches this presentation field. A
    whitespace-only value clears the field (surfaces fall back to the slug).
    Same strictness and atomicity contract as set_pai_model; the caller emits
    `kernel:reload_config` so reconcile projects it into spec.yaml.
    """
    display_name = display_name.strip()
    p = path or CONFIG_PATH
    data = _load_yaml(p) if p.exists() else {}
    pais = data.get("pais") if isinstance(data, dict) else None
    entry = None
    if isinstance(pais, list):
        entry = next(
            (e for e in pais if isinstance(e, dict) and e.get("name") == name), None
        )
    if entry is None:
        raise ValueError(f"unknown pai: {name!r}")
    if display_name:
        entry["display_name"] = display_name
    else:
        entry.pop("display_name", None)
    tmp = p.with_suffix(p.suffix + ".tmp")
    with tmp.open("w") as f:
        yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True)
    tmp.rename(p)
    return {"name": name, "display_name": display_name}


def set_pai_heartbeat(
    name: str, heartbeat: str | None, path: Path | None = None
) -> dict:
    """Write `heartbeat:` on one fleet entry (idle-relative wake interval).

    Accepts a duration string ("30m"/"1h") or bare int seconds; None/blank
    removes the key (heartbeat off — the default). Validates via the same
    parse the config loader applies, so a bad value fails loud here instead
    of breaking the next reconcile. Same strictness and atomicity contract
    as set_pai_display_name; the caller emits `kernel:reload_config` so
    reconcile projects it into spec.yaml and the proc watcher re-arms.
    """
    if isinstance(heartbeat, str):
        heartbeat = heartbeat.strip() or None
    if heartbeat is not None:
        secs = T.parse_duration(heartbeat)  # ValueError on junk — caller's 400
        if secs < 60:
            raise ValueError(f"heartbeat must be at least 60s, got {heartbeat!r}")
    p = path or CONFIG_PATH
    data = _load_yaml(p) if p.exists() else {}
    pais = data.get("pais") if isinstance(data, dict) else None
    entry = None
    if isinstance(pais, list):
        entry = next(
            (e for e in pais if isinstance(e, dict) and e.get("name") == name), None
        )
    if entry is None:
        raise ValueError(f"unknown pai: {name!r}")
    if heartbeat is not None:
        entry["heartbeat"] = heartbeat
    else:
        entry.pop("heartbeat", None)
    tmp = p.with_suffix(p.suffix + ".tmp")
    with tmp.open("w") as f:
        yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True)
    tmp.rename(p)
    return {"name": name, "heartbeat": heartbeat}


def capability_flags(path: Path | None = None) -> dict[str, bool]:
    """Back-compat predicate: True iff the capability lets the PAI send
    *directly and autonomously* (mode `yes`). Both `no` and `ask` are
    False here — in ask mode the PAI cannot send on its own; an approved
    item is carried by the approvals driver, not by clearing this flag. This is
    what `project_capabilities` reads to decide the per-driver freeze, so the
    freeze stays ON for no+ask and clears only for yes."""
    return {k: m == "yes" for k, m in capability_modes(path).items()}


def project_capabilities(path: Path | None = None) -> None:
    """Project capability modes into per-driver freeze files.

    yes      → remove the freeze file (driver sends directly).
    no / ask → write the freeze file with a reason (driver refuses the
               PAI's direct outbound). In ask mode the approvals driver
               still delivers approved items via its own trusted path; the
               freeze only blocks the PAI from sending on its own.

    The freeze file is the hard enforcement boundary the drivers check on every
    send; this keeps it in lockstep with `capabilities:` on boot and on every
    `kernel:reload_config`, so changing a mode in config.yaml takes effect
    without touching driver state by hand."""
    # Pre-split leftover: cowork used one capture.freeze for all facets. The
    # per-facet files below are the only gate now; a stale one would just
    # confuse whoever reads the dir, so drop it.
    try:
        (paths.sys_drivers("cowork") / "capture.freeze").unlink()
    except OSError:
        pass
    modes = capability_modes(path)
    for flag, spec in CAPABILITY_SPECS.items():
        if spec.get("driver") is None:
            # Kernel-enforced gate (bash_exec) — no freeze file to project;
            # boot.bash_gate reads the mode live at each tool dispatch.
            continue
        mode = modes.get(flag, "no")
        freeze = paths.sys_drivers(spec["driver"]) / spec["freeze"]
        if mode == "yes":
            try:
                freeze.unlink()
            except FileNotFoundError:
                pass
            except OSError as e:
                print(f"[kernel] capability: could not clear {freeze}: {e}", flush=True)
            else:
                print(f"[kernel] capability: {flag} granted (yes) — direct sends enabled", flush=True)
        else:
            try:
                freeze.parent.mkdir(parents=True, exist_ok=True)
                freeze.write_text(
                    f"frozen by capabilities.{flag}={mode} in etc/config.yaml\n"
                )
            except OSError as e:
                print(f"[kernel] capability: could not write {freeze}: {e}", flush=True)
            else:
                if mode == "ask":
                    print(f"[kernel] capability: {flag} ask — direct sends frozen, approvals queue active", flush=True)


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
            prompt_dir=spec.get("prompt_dir"),
            boilerplate=spec.get("boilerplate"),
            provider=spec.get("provider"),
            model=spec.get("model"),
            wake_on=spec.get("wake_on"),
            fallback=spec.get("fallback"),
            parent=spec.get("parent"),
            heartbeat=spec.get("heartbeat"),
        )
        try:
            P.append_log(name, "kernel: spawned via reconcile")
        except P.ProcessNotFound:
            pass
        print(f"[kernel] reconcile: spawned pai {name!r} (pid={pid})", flush=True)

    # Removed.
    for name in sorted(actual_names - desired_names):
        # Subagents are owned by their parent, not the top-level fleet
        # config — skip them so reconcile doesn't cancel children just
        # because they're absent from /etc/config.yaml.
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

    # Project owner-granted send capabilities into driver freeze files. Done
    # here so it runs on both boot (phases/reconcile) and `kernel:reload_config`
    # — the two paths that call this function — keeping enforcement in lockstep
    # with the declared flags.
    project_capabilities(path)
