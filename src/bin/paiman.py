#!/usr/bin/env python
"""paiman — PAI Package Manager.

Mutable installs into `/opt/paiman/<name>/` with FHS activation symlinks.
Five bundle kinds:

    bin     -> /usr/bin/<name>            (file symlink to entrypoint)
    driver  -> /usr/lib/drivers/<name>/   (dir symlink)
    skill   -> /usr/lib/skills/<name>/    (dir symlink, contains SKILL.md)
    prompt  -> /usr/share/prompts/<name>.md (file symlink)
    pai     -> /usr/lib/pais/<name>/      (dir symlink)

Sources:

    paiman install <name>                  resolve from the registry (default)
    paiman install <local/path>            install from a local directory
    paiman install <git-url>[@ref]         clone and install

The registry is `$PAIMAN_REGISTRY` (default
`https://github.com/whitematterlabs/pairegistry`) — either a git URL of a
flat-layout repo (`<name>/package.yaml`) or a local directory of the same
shape. Pai bundles list their deps in `deps:` as bare names; missing deps
are fetched from the registry recursively.

Other commands:

    paiman remove <name>                  uninstall (refuses if a pai bundle depends on it)
    paiman list                           list installed bundles
    paiman show <name>                    print package.yaml
    paiman init <name> [--type pai|subagent]   scaffold a new bundle template (legacy)
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

from boot import paths


# ---------- legacy scaffold (init / list / show for pai/subagent) ----------

PAI_PACKAGE_YAML_TEMPLATE = """\
# Auto-scaffolded by `paiman init {name}`. Edit fields marked CHANGE THIS.
name: {name}
kind: pai
version: 0.1.0

# CHANGE THIS — one-liner describing what this PAI does.
description: ""

# Default provider/model. Overridable per-instance at paiadd time.
provider: anthropic
model: claude-sonnet-4-6

# Role prompt path, relative to this bundle dir.
prompt: prompt.md

# CHANGE THIS — fnmatch globs over event `kind:` strings. The kernel
# nudges this PAI when an event's kind matches any glob. Available kinds
# come from /usr/lib/drivers/<driver>/events.yaml plus the kernel:* namespace.
# Examples:
#   wake_on: ['gmail:*']            # every gmail driver event
#   wake_on: ['imessage:new']       # one specific kind
#   wake_on: ['gmail:*', 'cal:*']   # multiple globs
# Leave [] only if this PAI is a `fallback` (catches unrouted events).
wake_on: []

# paiman-installed primitives this bundle pulls in (driver / bin / skill names).
# Listed drivers are auto-mounted into this PAI's home view
# (mounted set = deps ∩ installed-drivers).
deps: []

# Per-PAI home view extras (beyond universal bin/inbox/workspace/memory).
# Each entry: link: <path-under-$HOME>, target: <path-relative-to-PAI_ROOT>
# home:
#   links:
#     - link: communication/email
#       target: var/spool/communication/email
"""

SUBAGENT_PACKAGE_YAML_TEMPLATE = """\
# Auto-scaffolded by `paiman init {name} --type subagent`. Edit fields marked CHANGE THIS.
name: {name}
kind: subagent
version: 0.1.0

# CHANGE THIS — one-liner describing what this subagent specializes in.
description: ""

provider: anthropic
model: claude-sonnet-4-6

prompt: prompt.md

# Subagent bundles are referenced from a parent's `dependencies:` via
# `package: {name}`. They have no wake_on / fallback — the parent
# addresses them directly via bin/send-message, not the kernel router.
"""

PAI_PROMPT_MD_TEMPLATE = """\
# {name}

Role prompt for the {name} PAI.
"""

SUBAGENT_PROMPT_MD_TEMPLATE = """\
# {name}

Role prompt for the {name} persistent subagent. You are a long-lived
specialist child of your parent PAI. Describe the steady-state behavior
the parent should expect from you here.
"""


SCAFFOLD_TYPES = {
    "pai": (paths.usr_lib_pais, PAI_PACKAGE_YAML_TEMPLATE, PAI_PROMPT_MD_TEMPLATE),
    "subagent": (
        paths.usr_lib_subagents,
        SUBAGENT_PACKAGE_YAML_TEMPLATE,
        SUBAGENT_PROMPT_MD_TEMPLATE,
    ),
}


# ---------- install / remove ----------

INSTALLABLE_KINDS = ("bin", "driver", "skill", "prompt", "pai", "lib", "subagent")
PRIMITIVE_KINDS = ("bin", "driver", "skill", "prompt", "lib")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
COPY_IGNORE = shutil.ignore_patterns(".git", "__pycache__", ".DS_Store", "*.pyc")
DEFAULT_REGISTRY = "https://github.com/whitematterlabs/pairegistry"
# Bare-name resolution precedence when the same name lives under multiple
# topics. Bundles that pull deps (pais, drivers) outrank the leaf packages
# they depend on (bins, libs), so `paiman install ax` lands drivers/ax — which
# pulls in its bin/ax dep and builds the sidecar — not bin/ax on its own.
_TOPIC_RANK = {t: i for i, t in enumerate(
    ("pais", "drivers", "subagents", "skills", "prompts", "lib", "bin", "sbin")
)}


def _validate_name(name: str) -> None:
    if not name:
        raise SystemExit("paiman: name must be non-empty")
    if not NAME_RE.match(name) or name.startswith("."):
        raise SystemExit(f"paiman: invalid name {name!r}")


def _opt_rel(kind: str, name: str, topic: str | None) -> str:
    """Staging path under /opt/paiman/ for a bundle. Skills group by topic
    (`<topic>/<name>`); most other kinds group by kind (`<kind>/<name>`).
    Kind-grouping lets two different-kind bundles share a name without
    clobbering each other's staging dir — e.g. the `ax` driver and the `ax`
    bin client coexist at `driver/ax` and `bin/ax`. The scanners
    (`_find_installed_bundle`, `_iter_installed_bundles`, boot hooks) already
    walk one level deep, so this needs no reader changes.

    Exception: `prompt` bundles stay flat at `opt/paiman/<name>`. Unlike
    every other kind, a prompt's *bundle dir* (not just its activation slot)
    is referenced externally — `config.yaml` points `prompt_dir` at
    `opt/paiman/<name>` so bootstrap can glob its `*.md`. Grouping it by kind
    would silently empty the PAI's role prompt."""
    if topic:
        return f"{topic}/{name}"
    if kind == "prompt":
        return name
    return f"{kind}/{name}"


def _activation_slot(
    kind: str,
    name: str,
    entrypoint: str | None,
    topic: str | None = None,
) -> tuple[Path, Path]:
    """Return (slot_path, symlink_target) for the activation symlink."""
    rel = _opt_rel(kind, name, topic)
    bundle_dir = paths.opt_paiman() / rel
    if kind == "bin":
        if not entrypoint:
            raise SystemExit("paiman: bin bundle requires entrypoint")
        return paths.usr_bin() / name, bundle_dir / entrypoint
    if kind == "driver":
        return paths.usr_lib_drivers() / name, bundle_dir
    if kind == "skill":
        if topic:
            return paths.usr_lib_skills() / topic / name, bundle_dir
        return paths.usr_lib_skills() / name, bundle_dir
    if kind == "prompt":
        if not entrypoint:
            raise SystemExit("paiman: prompt bundle requires entrypoint")
        return paths.usr_share_prompts() / f"{name}.md", bundle_dir / entrypoint
    if kind == "pai":
        return paths.usr_lib_pais() / name, bundle_dir
    if kind == "subagent":
        # Persistent subagents resolve from /usr/lib/subagents/<name>/ (see
        # src/bin/subagent.py). Same bundle-dir symlink model as pai/driver.
        return paths.usr_lib_subagents() / name, bundle_dir
    if kind == "lib":
        # Python package import: /usr/lib/ is on sys.path; the slot is the
        # package dir itself so `from <name> import ...` resolves into the
        # bundle's `__init__.py`.
        return paths.usr_lib() / name, bundle_dir
    raise SystemExit(f"paiman: unsupported kind {kind!r}")


def _atomic_symlink(target: Path, slot: Path) -> None:
    slot.parent.mkdir(parents=True, exist_ok=True)
    tmp = slot.with_name(slot.name + ".paiman-tmp")
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    os.symlink(target, tmp)
    os.replace(tmp, slot)


def _is_url(s: str) -> bool:
    return (
        s.startswith(("http://", "https://", "git+", "git@"))
        or s.startswith("github.com/")
        or s.startswith("gitlab.com/")
    )


def _clone(url: str, into: Path) -> Path:
    """Shallow-clone `url` (with optional @ref) into `into`."""
    if "@" in url and not url.startswith("git@"):
        url, ref = url.rsplit("@", 1)
    else:
        ref = None
    if url.startswith(("github.com/", "gitlab.com/")):
        url = "https://" + url
    cmd = ["git", "clone", "--depth", "1"]
    if ref:
        cmd += ["--branch", ref]
    cmd += [url, str(into)]
    subprocess.run(cmd, check=True)
    return into


class _Registry:
    """Lazy handle to the registry. Caches the cloned/local path for the
    lifetime of one install invocation so deps can be resolved without
    re-cloning."""

    def __init__(self, work: Path) -> None:
        self._work = work
        self._path: Path | None = None

    def root(self) -> Path:
        if self._path is not None:
            return self._path
        loc = os.environ.get("PAIMAN_REGISTRY", DEFAULT_REGISTRY)
        if _is_url(loc):
            dest = self._work / "registry"
            self._path = _clone(loc, dest)
        else:
            p = Path(loc).expanduser()
            if not p.is_dir():
                raise SystemExit(f"paiman: registry {loc!r} is not a directory or URL")
            self._path = p.resolve()
        return self._path

    def lookup(self, name: str) -> Path:
        # Typed form `<topic>/<name>` (used by paifs-init to disambiguate
        # when a name appears under multiple topic folders, e.g.
        # `bin/browse` vs `subagents/browse`). Try direct first; then walk
        # one more level so kinds that themselves topic-fold their packages
        # (e.g. `skills/<topic>/<name>/`) still resolve.
        if "/" in name:
            candidate = self.root() / name
            if (candidate / "package.yaml").is_file():
                return candidate
            head, _, tail = name.partition("/")
            head_dir = self.root() / head
            if head_dir.is_dir():
                for child in sorted(head_dir.iterdir()):
                    if not child.is_dir():
                        continue
                    nested = child / tail
                    if (nested / "package.yaml").is_file():
                        return nested
            raise SystemExit(
                f"paiman: {name!r} not found in registry {self.root()}"
            )
        pkg_dir = self.root() / name
        if (pkg_dir / "package.yaml").is_file():
            return pkg_dir
        # Topic-foldered: <root>/<topic>/<name>/package.yaml. A bare name can
        # collide across topics — e.g. drivers/ax and the bin/ax client it
        # depends on. Walk topics in precedence order so the umbrella bundle
        # wins; plain alphabetical order would pick bin/ax (b < d) and never
        # the driver that builds the sidecar.
        topics = sorted(
            (d for d in self.root().iterdir()
             if d.is_dir() and not d.name.startswith(".")),
            key=lambda d: (_TOPIC_RANK.get(d.name, len(_TOPIC_RANK)), d.name),
        )
        for topic_dir in topics:
            candidate = topic_dir / name
            if (candidate / "package.yaml").is_file():
                return candidate
        raise SystemExit(
            f"paiman: {name!r} not found in registry {self.root()}"
        )


def _resolve_source(arg: str, registry: _Registry, work: Path) -> Path:
    """Map a CLI source argument to an on-disk source tree with package.yaml."""
    if _is_url(arg):
        return _clone(arg, work / "url-src")
    p = Path(arg).expanduser()
    if p.is_dir():
        return p.resolve()
    # Bare name → registry lookup.
    return registry.lookup(arg)


def _load_manifest(src: Path) -> dict:
    pkg = src / "package.yaml"
    if not pkg.is_file():
        raise SystemExit(f"paiman: {pkg} not found")
    try:
        with pkg.open() as f:
            data = yaml.safe_load(f) or {}
    except yaml.YAMLError as e:
        raise SystemExit(f"paiman: invalid package.yaml: {e}") from e
    if not isinstance(data, dict):
        raise SystemExit("paiman: package.yaml must be a mapping")
    return data


def _audit_log(line: str) -> None:
    log_dir = paths.var_lib_paiman()
    log_dir.mkdir(parents=True, exist_ok=True)
    ts = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    with (log_dir / "log.md").open("a") as f:
        f.write(f"- {ts}  {line}\n")


def _install_from_source(src: Path, src_arg: str, registry: _Registry, work: Path,
                         seen: set[str], kinds_out: set[str] | None = None) -> str:
    """Install one bundle from a resolved source tree. Returns the bundle name.
    Recursive for pai bundles via their `deps:` list. `kinds_out`, if given,
    collects every kind installed during this call (including transitive deps)
    so the caller can decide whether to emit a kernel reload event."""
    manifest = _load_manifest(src)
    name = manifest.get("name")
    kind = manifest.get("kind")
    entrypoint = manifest.get("entrypoint")
    topic = manifest.get("topic") if kind == "skill" else None
    if not name or not isinstance(name, str):
        raise SystemExit(f"paiman: package.yaml at {src} missing 'name'")
    _validate_name(name)
    if kind not in INSTALLABLE_KINDS:
        raise SystemExit(
            f"paiman: kind {kind!r} not installable "
            f"(known: {', '.join(INSTALLABLE_KINDS)})"
        )
    # Key cycle detection on the resolved source path, not the bundle name.
    # Sibling bundles of different kinds may legitimately share a name
    # (e.g. the `ax` driver depends on the `ax` bin client); only a true
    # cycle revisits the same source tree.
    src_key = str(src.resolve())
    if src_key in seen:
        raise SystemExit(f"paiman: dependency cycle detected at {name!r}")
    seen.add(src_key)

    if kind in ("bin", "prompt"):
        if not entrypoint:
            raise SystemExit(f"paiman: {kind} bundle requires 'entrypoint'")
        if not (src / entrypoint).is_file():
            raise SystemExit(f"paiman: entrypoint {entrypoint!r} not found in source")

    # Resolve declared `deps:` for any bundle that has them. Drivers
    # commonly depend on shared lib packages (e.g. tailer); pai bundles
    # pull in their drivers/skills. Fail fast before touching disk.
    if kind in ("pai", "driver"):
        deps = manifest.get("deps") or []
        if not isinstance(deps, list):
            raise SystemExit(f"paiman: {kind} bundle 'deps' must be a list of names")
        for dep in deps:
            if not isinstance(dep, str):
                raise SystemExit(f"paiman: dep entries must be strings, got {dep!r}")
            if _find_installed_bundle(dep) is not None:
                continue  # already installed; mutable, leave it alone
            dep_src = registry.lookup(dep)
            _install_from_source(dep_src, dep, registry, work, seen, kinds_out)

    # Clean up old flat install if migrating into a topic subdir.
    if topic:
        old_opt = paths.opt_paiman() / name
        if old_opt.is_symlink():
            old_opt.unlink()
        elif old_opt.is_dir():
            shutil.rmtree(old_opt)
        old_link = paths.usr_lib_skills() / name
        if old_link.is_symlink():
            old_link.unlink()
        elif old_link.is_dir():
            shutil.rmtree(old_link)

    # Copy to /opt/paiman/<topic-or-kind>/<name>/ (overwrite).
    dest = paths.opt_paiman() / _opt_rel(kind, name, topic)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() or dest.is_symlink():
        shutil.rmtree(dest)
    shutil.copytree(src, dest, ignore=COPY_IGNORE, symlinks=False)

    # Activate.
    slot, target = _activation_slot(kind, name, entrypoint, topic=topic)
    if kind == "bin":
        # Write a shell shim that execs the entrypoint via the interpreter
        # paiman itself runs under: the kernel venv python in dev, the embedded
        # python in a bundled PAI.app. A bare symlink would fall back to the
        # bin's own shebang (`#!/usr/bin/env python`), which can't portably
        # reference either interpreter and breaks when no `python` is on PATH.
        slot.parent.mkdir(parents=True, exist_ok=True)
        if slot.is_symlink() or slot.exists():
            slot.unlink()
        slot.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{target}" "$@"\n')
        slot.chmod(0o755)
    else:
        _atomic_symlink(target, slot)

    _audit_log(f"install {kind} {name} from {src_arg}")
    print(f"installed {kind} {name} -> {slot}")
    if kinds_out is not None:
        kinds_out.add(kind)

    # Run install hooks. Failures are logged but do not abort — a bad
    # hook should not leave the bundle half-uninstalled. Boot hooks are
    # the kernel's responsibility (see src/boot/phases/hooks.py).
    hooks = manifest.get("hooks") or {}
    if isinstance(hooks, dict):
        install_cmds = hooks.get("install") or []
        if isinstance(install_cmds, str):
            install_cmds = [install_cmds]
        for cmd in install_cmds:
            if not isinstance(cmd, str) or not cmd.strip():
                continue
            print(f"  hook[install]: {cmd}")
            try:
                rc = subprocess.run(
                    cmd, shell=True, cwd=str(paths.PAI_ROOT), timeout=120
                ).returncode
            except (OSError, subprocess.TimeoutExpired) as e:
                print(f"  hook[install]: FAILED — {e}")
                continue
            if rc != 0:
                print(f"  hook[install]: rc={rc}")
    return name


def cmd_install(args: argparse.Namespace) -> int:
    src_arg: str = args.source
    installed_kinds: set[str] = set()
    with tempfile.TemporaryDirectory(prefix="paiman-") as tmp:
        work = Path(tmp)
        registry = _Registry(work)
        src = _resolve_source(src_arg, registry, work)
        _install_from_source(
            src, src_arg, registry, work, seen=set(), kinds_out=installed_kinds
        )
    # Re-stitch all running PAIs' homes if anything that affects them landed.
    # Skill/prompt installs need to surface in `memory/skills/` and prompt
    # blocks without a reboot. Bin/lib are picked up via PATH/sys.path on the
    # PAI's next turn — no reload needed. Driver/pai installs are followed by
    # explicit paictl/paiadd which emit reload themselves.
    if installed_kinds & {"skill", "prompt"} and not getattr(args, "no_reload", False):
        try:
            from boot import processes as _processes
            _processes.emit_event({"kind": "kernel:reload_config", "source": "paiman",
                                   "action": "install", "source_arg": src_arg})
        except Exception as e:
            print(f"paiman: warning — could not emit kernel:reload_config: {e}",
                  file=sys.stderr)
    return 0


def _iter_installed_bundles() -> list[tuple[str, Path]]:
    """Return (name, bundle_dir) for every installed bundle, flat or
    topic-foldered (`<opt>/<topic>/<name>/package.yaml`)."""
    out: list[tuple[str, Path]] = []
    root = paths.opt_paiman()
    if not root.exists():
        return out
    for entry in sorted(root.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        if (entry / "package.yaml").is_file():
            out.append((entry.name, entry))
            continue
        for sub in sorted(entry.iterdir()):
            if not sub.is_dir() or sub.name.startswith("."):
                continue
            if (sub / "package.yaml").is_file():
                out.append((sub.name, sub))
    return out


def _bundles_depending_on(name: str) -> list[str]:
    """Return the names of installed pai bundles that list `name` in their deps."""
    out: list[str] = []
    for bname, bdir in _iter_installed_bundles():
        if bname == name:
            continue
        pkg = bdir / "package.yaml"
        try:
            with pkg.open() as f:
                data = yaml.safe_load(f) or {}
        except yaml.YAMLError:
            continue
        if data.get("kind") != "pai":
            continue
        if name in (data.get("deps") or []):
            out.append(bname)
    return out


def _find_installed_bundle(name: str) -> Path | None:
    """Return the on-disk bundle dir for `name`, flat or topic-foldered."""
    flat = paths.opt_paiman() / name
    if (flat / "package.yaml").is_file():
        return flat
    root = paths.opt_paiman()
    if not root.exists():
        return None
    for entry in sorted(root.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        if (entry / "package.yaml").is_file():
            continue  # leaf bundle, not a topic dir
        candidate = entry / name
        if (candidate / "package.yaml").is_file():
            return candidate
    return None


def cmd_remove(args: argparse.Namespace) -> int:
    name: str = args.name
    _validate_name(name)
    bundle_dir = _find_installed_bundle(name)
    if bundle_dir is None or not bundle_dir.is_dir():
        raise SystemExit(f"paiman: {name!r} is not installed")
    dependents = _bundles_depending_on(name)
    if dependents and not args.force:
        raise SystemExit(
            f"paiman: cannot remove {name!r}; required by pai bundle(s): "
            f"{', '.join(dependents)} (use --force to override)"
        )
    manifest = _load_manifest(bundle_dir)
    kind = manifest.get("kind")
    entrypoint = manifest.get("entrypoint")
    topic = manifest.get("topic") if kind == "skill" else None
    if kind in INSTALLABLE_KINDS:
        slot, _ = _activation_slot(kind, name, entrypoint, topic=topic)
        if slot.is_symlink() or slot.exists():
            slot.unlink()
    shutil.rmtree(bundle_dir)
    _audit_log(f"remove {kind} {name}")
    print(f"removed {kind} {name}")
    return 0


# ---------- list / show ----------

def _iter_legacy_bundles(bundle_type: str) -> list[tuple[str, dict]]:
    root_fn, _, _ = SCAFFOLD_TYPES[bundle_type]
    root = root_fn()
    if not root.exists():
        return []
    out: list[tuple[str, dict]] = []
    for entry in sorted(root.iterdir()):
        if entry.is_symlink():
            continue
        pkg = entry / "package.yaml"
        if not pkg.exists():
            continue
        try:
            with pkg.open() as f:
                data = yaml.safe_load(f) or {}
        except yaml.YAMLError as e:
            data = {"_error": str(e)}
        out.append((entry.name, data))
    return out


def _iter_installed() -> list[tuple[str, dict]]:
    out: list[tuple[str, dict]] = []
    for name, bdir in _iter_installed_bundles():
        pkg = bdir / "package.yaml"
        try:
            with pkg.open() as f:
                data = yaml.safe_load(f) or {}
        except yaml.YAMLError as e:
            data = {"_error": str(e)}
        out.append((name, data))
    return out


def cmd_list(args: argparse.Namespace) -> int:
    installed = _iter_installed()
    print("installed (paiman):")
    if not installed:
        print("  (none)")
    else:
        for name, data in installed:
            if "_error" in data:
                print(f"  {name}  [parse error: {data['_error']}]")
                continue
            kind = data.get("kind", "?")
            version = data.get("version", "?")
            print(f"  {name}  [{kind} {version}]")

    for bundle_type in ("pai", "subagent"):
        bundles = _iter_legacy_bundles(bundle_type)
        if not bundles:
            continue
        print(f"{bundle_type}s (scaffolded):")
        for name, data in bundles:
            if "_error" in data:
                print(f"  {name}  [parse error: {data['_error']}]")
                continue
            desc = (data.get("description") or "").strip() or "(no description)"
            provider = data.get("provider") or "?"
            model = data.get("model")
            tail = f"{provider}" + (f"/{model}" if model else "")
            print(f"  {name}  [{tail}]  {desc}")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    name: str = args.name
    candidates: list[Path] = []
    bundle_dir = _find_installed_bundle(name)
    if bundle_dir is not None:
        candidates.append(bundle_dir / "package.yaml")
    for bundle_type in ("pai", "subagent"):
        root_fn, _, _ = SCAFFOLD_TYPES[bundle_type]
        candidates.append(root_fn() / name / "package.yaml")
    for pkg in candidates:
        if pkg.exists():
            print(f"# {pkg}")
            print(pkg.read_text(), end="")
            return 0
    raise SystemExit(f"paiman: bundle {name!r} not found")


def _iter_registry(root: Path) -> list[tuple[str, dict, Path]]:
    """Walk the registry root looking for bundles. Handles both layouts:
    flat (`<root>/<name>/package.yaml`) and kind-foldered
    (`<root>/<kind>/<name>/package.yaml`). Returns (name, manifest, path)."""
    out: list[tuple[str, dict, Path]] = []
    # Dedupe on (kind, name) so a single name can legitimately appear under
    # multiple kinds (e.g. a `bin/browse` verb and a `subagents/browse`
    # bundle that teaches PAIs to use it). Falling back to name-only when
    # kind is missing keeps the old behavior for malformed package.yaml.
    seen: set[tuple[str, str]] = set()
    for entry in sorted(root.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        pkg = entry / "package.yaml"
        if pkg.is_file():
            try:
                with pkg.open() as f:
                    data = yaml.safe_load(f) or {}
            except yaml.YAMLError as e:
                data = {"_error": str(e)}
            key = (str(data.get("kind") or ""), entry.name)
            if key not in seen:
                seen.add(key)
                out.append((entry.name, data, entry))
            continue
        for sub in sorted(entry.iterdir()):
            if not sub.is_dir():
                continue
            spkg = sub / "package.yaml"
            if spkg.is_file():
                try:
                    with spkg.open() as f:
                        data = yaml.safe_load(f) or {}
                except yaml.YAMLError as e:
                    data = {"_error": str(e)}
                key = (str(data.get("kind") or ""), sub.name)
                if key not in seen:
                    seen.add(key)
                    out.append((sub.name, data, sub))
                continue
            # Topic-nested layout (e.g. skills/<topic>/<name>/package.yaml):
            # `sub` is a topic dir, walk one level deeper.
            for leaf in sorted(sub.iterdir()):
                if not leaf.is_dir():
                    continue
                lpkg = leaf / "package.yaml"
                if not lpkg.is_file():
                    continue
                try:
                    with lpkg.open() as f:
                        data = yaml.safe_load(f) or {}
                except yaml.YAMLError as e:
                    data = {"_error": str(e)}
                key = (str(data.get("kind") or ""), leaf.name)
                if key not in seen:
                    seen.add(key)
                    out.append((leaf.name, data, leaf))
    return out


def cmd_search(args: argparse.Namespace) -> int:
    """List bundles available in the registry. Clones if registry is a URL."""
    pattern: str | None = (args.pattern or "").lower() or None
    kind_filter: str | None = args.kind
    with tempfile.TemporaryDirectory(prefix="paiman-") as tmp:
        work = Path(tmp)
        registry = _Registry(work)
        try:
            root = registry.root()
        except SystemExit as e:
            raise e
        bundles = _iter_registry(root)
        loc = os.environ.get("PAIMAN_REGISTRY", DEFAULT_REGISTRY)
        print(f"available (registry: {loc}):")
        printed = 0
        for name, data, _ in bundles:
            if "_error" in data:
                if kind_filter or pattern:
                    continue
                print(f"  {name}  [parse error: {data['_error']}]")
                printed += 1
                continue
            kind = data.get("kind", "?")
            if kind_filter and kind != kind_filter:
                continue
            if pattern and pattern not in name.lower():
                continue
            version = data.get("version", "?")
            desc = (data.get("description") or "").strip()
            tail = f"  — {desc}" if desc else ""
            print(f"  {name}  [{kind} {version}]{tail}")
            printed += 1
        if printed == 0:
            print("  (none)")
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    name: str = args.name
    bundle_type: str = args.type
    _validate_name(name)
    if bundle_type not in SCAFFOLD_TYPES:
        raise SystemExit(
            f"paiman: unknown --type {bundle_type!r} "
            f"(known: {', '.join(sorted(SCAFFOLD_TYPES))})"
        )
    root_fn, pkg_tmpl, prompt_tmpl = SCAFFOLD_TYPES[bundle_type]
    bundle_dir: Path = root_fn() / name
    if bundle_dir.exists():
        raise SystemExit(f"paiman: bundle already exists at {bundle_dir}")
    bundle_dir.mkdir(parents=True)
    (bundle_dir / "package.yaml").write_text(pkg_tmpl.format(name=name))
    (bundle_dir / "prompt.md").write_text(prompt_tmpl.format(name=name))
    print(f"scaffolded {bundle_type} bundle at {bundle_dir}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="paiman", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_install = sub.add_parser("install", help="install a bundle (registry name, local path, or git URL)")
    p_install.add_argument("source", help="bundle name in the registry, local directory path, or git URL (optionally @ref)")
    p_install.add_argument(
        "--no-reload",
        action="store_true",
        help="skip the post-install kernel:reload_config emit; for batch "
        "callers (paisetup) that emit a single reload after the whole batch "
        "instead of one reconcile storm per package",
    )
    p_install.set_defaults(func=cmd_install)

    p_remove = sub.add_parser("remove", help="remove an installed bundle")
    p_remove.add_argument("name", help="bundle name")
    p_remove.add_argument("--force", action="store_true", help="remove even if a pai bundle depends on it")
    p_remove.set_defaults(func=cmd_remove)

    p_list = sub.add_parser("list", help="list installed bundles")
    p_list.set_defaults(func=cmd_list)

    p_show = sub.add_parser("show", help="print package.yaml")
    p_show.add_argument("name", help="bundle name")
    p_show.set_defaults(func=cmd_show)

    p_search = sub.add_parser(
        "search",
        help="list bundles available in the registry (clones if registry is a URL)",
    )
    p_search.add_argument("pattern", nargs="?", help="optional substring filter on bundle name")
    p_search.add_argument("--kind", help="filter by kind (driver, skill, pai, bin, prompt)")
    p_search.set_defaults(func=cmd_search)

    p_init = sub.add_parser("init", help="scaffold a new bundle template (legacy pai/subagent)")
    p_init.add_argument("name", help="bundle name (e.g., email-pai)")
    p_init.add_argument(
        "--type",
        default="pai",
        choices=sorted(SCAFFOLD_TYPES),
        help="bundle type (default: pai)",
    )
    p_init.set_defaults(func=cmd_init)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
