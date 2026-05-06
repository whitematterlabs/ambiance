#!/usr/bin/env python
"""paiman — PAI Package Manager.

Mutable installs into `/opt/paiman/<name>/` with FHS activation symlinks.
Seven bundle kinds:

    bin     -> /usr/bin/<name>             (file symlink to entrypoint)
    sbin    -> /sbin/<name>                (file symlink to entrypoint)
    driver  -> /usr/lib/drivers/<name>/    (dir symlink)
    skill   -> /usr/lib/skills/<name>/     (dir symlink, contains SKILL.md)
    prompt  -> /usr/share/prompts/<name>.md (file symlink)
    pai     -> /usr/lib/pais/<name>/       (dir symlink)
    lib     -> /usr/lib/<name>/            (dir symlink)

Sources:

    paiman install <name>                  resolve from the registry (default)
    paiman install <local/path>            install from a local directory
    paiman install <git-url>[@ref]         clone and install

The registry is `$PAIMAN_REGISTRY` (default
`https://github.com/whitematterlabs/pairegistry`) — either a git URL or a
local directory in the typed-root layout (`drivers/<name>/`, `bin/<name>/`,
`sbin/<name>/`, `lib/<name>/`, `skills/<name>/`, `prompts/<name>/`,
`pais/<name>/`), each with its own `package.yaml`. Bundles list their
deps in `deps:` as bare names. Each entry is resolved registry-first
(installed recursively); names not found in the registry are treated
as PyPI packages and pip-installed into the kernel venv at
`/usr/lib/venv/` once the bundle install completes.

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
kind: pai
description: ""
prompt: usr/lib/pais/{name}/prompt.md
provider: anthropic
# model: claude-sonnet-4-6
#
# wake_on: list of fnmatch globs over event `kind:` strings. The kernel
# nudges this PAI when an event's kind matches any glob. Available kinds
# come from /usr/lib/drivers/<driver>/events.yaml plus the kernel:* namespace.
# Examples:
#   wake_on: ['gmail:*']            # every gmail driver event
#   wake_on: ['imessage:new']       # one specific kind
#   wake_on: ['gmail:*', 'cal:*']   # multiple globs
# Omit or leave empty if this PAI is only a `fallback` (catches unrouted).
# wake_on: []
#
# deps: paiman-installed primitives this PAI bundle pulls in. Resolved
# from the registry on `paiman install`.
# deps: []
"""

SUBAGENT_PACKAGE_YAML_TEMPLATE = """\
kind: subagent
description: ""
prompt: usr/lib/subagents/{name}/prompt.md
provider: anthropic
# model: claude-sonnet-4-6
#
# Subagent bundles are referenced from a parent's dependencies: entry
# via `package: {name}`. They have no wake_on/fallback — the parent
# addresses them directly via bin/nudge, not the kernel router.
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

INSTALLABLE_KINDS = ("bin", "sbin", "driver", "skill", "prompt", "pai", "lib", "subagent")
PRIMITIVE_KINDS = ("bin", "sbin", "driver", "skill", "prompt", "lib")
TYPED_ROOTS = ("drivers", "bin", "sbin", "lib", "skills", "prompts", "pais", "subagents")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
COPY_IGNORE = shutil.ignore_patterns(".git", "__pycache__", ".DS_Store", "*.pyc")
DEFAULT_REGISTRY = "https://github.com/whitematterlabs/pairegistry"


def _validate_name(name: str) -> None:
    if not name:
        raise SystemExit("paiman: name must be non-empty")
    if not NAME_RE.match(name) or name.startswith("."):
        raise SystemExit(f"paiman: invalid name {name!r}")


def _activation_slot(
    kind: str,
    name: str,
    entrypoint: str | None,
    topic: str | None = None,
) -> tuple[Path, Path]:
    """Return (slot_path, symlink_target) for the activation symlink."""
    rel = f"{topic}/{name}" if topic else name
    bundle_dir = paths.opt_paiman() / rel
    if kind == "bin":
        if not entrypoint:
            raise SystemExit("paiman: bin bundle requires entrypoint")
        return paths.usr_bin() / name, bundle_dir / entrypoint
    if kind == "sbin":
        if not entrypoint:
            raise SystemExit("paiman: sbin bundle requires entrypoint")
        return paths.sbin() / name, bundle_dir / entrypoint
    if kind == "lib":
        return paths.usr_lib() / name, bundle_dir
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
        return paths.usr_lib_subagents() / name, bundle_dir
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

    def lookup(self, name: str, *, required: bool = True) -> Path | None:
        for typed_root in TYPED_ROOTS:
            candidate = self.root() / typed_root / name
            if (candidate / "package.yaml").is_file():
                return candidate
        # Skills support a `<typed_root>/<topic>/<name>/` layout. Walk one
        # extra level under skills/ to find topic-foldered bundles.
        skills_root = self.root() / "skills"
        if skills_root.is_dir():
            for topic_dir in sorted(skills_root.iterdir()):
                if not topic_dir.is_dir() or topic_dir.name.startswith("."):
                    continue
                if (topic_dir / "package.yaml").is_file():
                    continue  # flat skill at this level, not a topic dir
                candidate = topic_dir / name
                if (candidate / "package.yaml").is_file():
                    return candidate
        if required:
            raise SystemExit(
                f"paiman: {name!r} not found in registry {self.root()}"
            )
        return None


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
                         seen: set[str], pip_deps: set[str]) -> str:
    """Install one bundle from a resolved source tree. Returns the bundle name.

    `deps:` entries are resolved registry-first; misses are accumulated into
    `pip_deps` for a single batch pip-install at the end of the top-level
    install."""
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
    if name in seen:
        raise SystemExit(f"paiman: dependency cycle detected at {name!r}")
    seen.add(name)

    if kind in ("bin", "sbin", "prompt"):
        if not entrypoint:
            raise SystemExit(f"paiman: {kind} bundle requires 'entrypoint'")
        if not (src / entrypoint).is_file():
            raise SystemExit(f"paiman: entrypoint {entrypoint!r} not found in source")

    # Resolve deps before touching disk so we fail fast. Each entry is tried
    # against the registry first; misses are queued as pip packages and
    # batch-installed into /usr/lib/venv/ at the end.
    deps = manifest.get("deps") or []
    if not isinstance(deps, list):
        raise SystemExit(f"paiman: {kind} bundle 'deps' must be a list of names")
    for dep in deps:
        if not isinstance(dep, str):
            raise SystemExit(f"paiman: dep entries must be strings, got {dep!r}")
        if _find_installed_bundle(dep) is not None:
            continue  # already installed as a bundle; leave it alone
        dep_src = registry.lookup(dep, required=False)
        if dep_src is not None:
            _install_from_source(dep_src, dep, registry, work, seen, pip_deps)
        else:
            pip_deps.add(dep)

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

    # Copy to /opt/paiman/[<topic>/]<name>/ (overwrite).
    dest = paths.opt_paiman() / (f"{topic}/{name}" if topic else name)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() or dest.is_symlink():
        shutil.rmtree(dest)
    shutil.copytree(src, dest, ignore=COPY_IGNORE, symlinks=False)

    # Activate.
    slot, target = _activation_slot(kind, name, entrypoint, topic=topic)
    _atomic_symlink(target, slot)
    if kind in ("bin", "sbin"):
        try:
            target.chmod(target.stat().st_mode | 0o111)
        except OSError:
            pass

    _audit_log(f"install {kind} {name} from {src_arg}")
    print(f"installed {kind} {name} -> {slot}")
    return name


def _venv_python() -> Path:
    return paths.usr_lib() / "venv" / "bin" / "python"


def _pip_install(packages: set[str]) -> None:
    """Install PyPI packages into the kernel venv."""
    if not packages:
        return
    py = _venv_python()
    if not py.exists():
        raise SystemExit(
            f"paiman: kernel venv python not found at {py} — "
            "run paifs-init to provision the venv before installing pip deps"
        )
    pkgs = sorted(packages)
    print(f"pip install (kernel venv): {', '.join(pkgs)}")
    subprocess.run(
        [str(py), "-m", "pip", "install", "--disable-pip-version-check", *pkgs],
        check=True,
    )
    _audit_log(f"pip install {' '.join(pkgs)}")


def cmd_install(args: argparse.Namespace) -> int:
    src_arg: str = args.source
    pip_deps: set[str] = set()
    with tempfile.TemporaryDirectory(prefix="paiman-") as tmp:
        work = Path(tmp)
        registry = _Registry(work)
        src = _resolve_source(src_arg, registry, work)
        _install_from_source(src, src_arg, registry, work, seen=set(), pip_deps=pip_deps)
    _pip_install(pip_deps)
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


def _find_installed_bundle(name: str) -> Path | None:
    """Return the on-disk bundle dir for `name`, flat or topic-foldered."""
    for bname, bdir in _iter_installed_bundles():
        if bname == name:
            return bdir
    return None


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
        if data.get("kind") not in ("pai", "subagent", "skill"):
            continue
        if name in (data.get("deps") or []):
            out.append(bname)
    return out


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
    seen: set[str] = set()
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
            if entry.name not in seen:
                seen.add(entry.name)
                out.append((entry.name, data, entry))
            continue
        for sub in sorted(entry.iterdir()):
            if not sub.is_dir() or sub.name.startswith("."):
                continue
            spkg = sub / "package.yaml"
            if spkg.is_file():
                try:
                    with spkg.open() as f:
                        data = yaml.safe_load(f) or {}
                except yaml.YAMLError as e:
                    data = {"_error": str(e)}
                if sub.name not in seen:
                    seen.add(sub.name)
                    out.append((sub.name, data, sub))
                continue
            # Topic-foldered layout (e.g. skills/<topic>/<name>/).
            for leaf in sorted(sub.iterdir()):
                if not leaf.is_dir() or leaf.name.startswith("."):
                    continue
                lpkg = leaf / "package.yaml"
                if not lpkg.is_file():
                    continue
                try:
                    with lpkg.open() as f:
                        data = yaml.safe_load(f) or {}
                except yaml.YAMLError as e:
                    data = {"_error": str(e)}
                if leaf.name not in seen:
                    seen.add(leaf.name)
                    out.append((leaf.name, data, leaf))
    return out


def cmd_search(args: argparse.Namespace) -> int:
    """List bundles available in the registry. Clones if registry is a URL."""
    pattern: str | None = (args.pattern or "").lower() or None
    kind_filter: str | None = args.kind
    with tempfile.TemporaryDirectory(prefix="paiman-") as tmp:
        work = Path(tmp)
        registry = _Registry(work)
        root = registry.root()
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
