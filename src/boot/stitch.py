"""Stitch a PAI's home — build the symlink view per v3 FILESYSTEM spec.

A PAI's home is a directory of symlinks pointing into:
  - the PAI's own instance state at /var/lib/instances/<slug>/
  - the canonical shared memory at /var/lib/memory/

The "root" PAI (pid 1) lives at /root/ — same shape, different slot.
Every other PAI lives at /home/<slug>/.

Stitch is idempotent: re-running heals broken/missing links. Existing
instance content is never overwritten.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml

from . import paths
from . import skills as _skills_filter

# Top-level home entries the kernel always seeds. A bundle's `home.links`
# may not collide with any of these.
RESERVED_HOME_LINKS: frozenset[str] = frozenset(
    {"bin", "inbox", "workspace", "memory", "tmp"}
)

# Bundleless PAIs (no `package:` in /etc/config.yaml) get their home extras
# from this map. root and pid-2 `pai` keep the universal `communication`
# view that the kernel used to ship for everyone.
_COMMUNICATION_LINK: tuple[str, Path] = (
    "communication",
    Path("var") / "spool" / "communication",
)
_BUNDLELESS_SEEDS: dict[str, tuple[tuple[str, Path], ...]] = {
    "root": (("sbin", Path("sbin")), _COMMUNICATION_LINK),
    "pai": (_COMMUNICATION_LINK,),
}


def _merge_real_dir_into(src: Path, dst: Path) -> bool:
    """Move real content out of `src` — a real dir wrongly sitting where a
    managed symlink to `dst` belongs — into `dst`, preserving data. Returns
    True iff `src` ends up empty, i.e. it is safe to `rmdir` it and lay down
    the symlink. Any entry we cannot move without discarding data is left in
    place and the function returns False, so the caller keeps the real dir and
    nothing is lost.

    This is the heal path for a clobbered universal link: a nested driver link
    (`communication/email`) can materialize `communication` as a real dir
    before the universal `communication → var/spool/communication` seed is
    laid, after which the kernel's own owner-thread writes
    (`communication/messages/me/<pid>/…`) accumulate in the unwatched real dir
    instead of the spool the message drivers tail.
    """
    clean = True
    for child in sorted(src.iterdir()):
        peer = dst / child.name
        # A symlink that merely re-exposes the target's own child (e.g. a
        # driver's `communication/email → …/communication/email`) becomes a
        # pure duplicate once `src` itself is the symlink — drop it.
        if child.is_symlink() and child.resolve(strict=False) == peer:
            child.unlink()
            continue
        if not (peer.is_symlink() or peer.exists()):
            os.rename(child, peer)
            continue
        # Both plain dirs — recurse.
        if (
            child.is_dir() and not child.is_symlink()
            and peer.is_dir() and not peer.is_symlink()
        ):
            if _merge_real_dir_into(child, peer):
                child.rmdir()
            else:
                clean = False
            continue
        # Destination is an empty placeholder file — source content wins.
        if (
            child.is_file() and not child.is_symlink()
            and peer.is_file() and not peer.is_symlink()
            and peer.stat().st_size == 0
        ):
            os.replace(child, peer)
            continue
        # Identical duplicate symlink.
        if (
            child.is_symlink() and peer.is_symlink()
            and child.readlink() == peer.readlink()
        ):
            child.unlink()
            continue
        clean = False
    return clean


def _stitch_links(home: Path, instance: Path, extra_links: tuple = ()) -> None:
    # Symlink targets are relative so the home tree is portable if PAI_ROOT moves.
    # Computed against the link's *physical* parent (resolve()) so links that
    # nest under a symlinked dir still get a correct target.
    inst_under_root = instance.relative_to(paths.PAI_ROOT)
    mem_under_root = paths.var_lib_memory().relative_to(paths.PAI_ROOT)
    doc_under_root = paths.usr_share_doc().relative_to(paths.PAI_ROOT)

    # `memory/skills` is NOT in this list — it's stitched as a directory of
    # per-skill symlinks (filtered by `visible_to:`) by `_stitch_skills`.
    links: tuple[tuple[str, Path], ...] = (
        ("bin", Path("usr") / "bin"),
        *extra_links,
        ("inbox", inst_under_root / "inbox"),
        ("workspace", inst_under_root / "workspace"),
        ("memory/private", inst_under_root / "memory" / "private"),
        ("memory/shared", mem_under_root),
        ("memory/doc", doc_under_root),
    )
    # Prune stale top-level symlinks the kernel no longer manages (e.g. the
    # universal `communication` link that bundle-aware homes drop). We only
    # touch symlinks, never real dirs or files.
    expected_top: set[str] = {rel.split("/", 1)[0] for rel, _ in links} | {"tmp"}
    for child in home.iterdir():
        if child.name not in expected_top and child.is_symlink():
            child.unlink()

    for rel, target_under_root in links:
        link = home / rel
        link.parent.mkdir(parents=True, exist_ok=True)
        target_abs = (paths.PAI_ROOT / target_under_root).resolve()
        # If the link's physical parent already exposes the target (e.g. the
        # parent is itself a symlink whose destination *is* the target's
        # parent), creating a symlink here would point at itself → ELOOP.
        link_physical = link.parent.resolve() / link.name
        if link_physical == target_abs:
            continue
        target = Path(os.path.relpath(target_abs, link.parent.resolve()))
        if link.is_symlink():
            if link.readlink() == target:
                continue
            link.unlink()
        elif link.exists():
            if not link.is_dir():
                continue  # a real file sits here — never clobber it
            if not any(link.iterdir()):
                link.rmdir()
            elif target_abs.is_dir() and _merge_real_dir_into(link, target_abs):
                # Non-empty real dir where a managed symlink belongs (a prior
                # stitch was clobbered). Content migrated into the shared
                # target above; drop the now-empty dir and lay the symlink so
                # stitch's heal contract holds.
                link.rmdir()
            else:
                continue
        link.symlink_to(target)
    (home / "tmp").mkdir(exist_ok=True)


def _stitch_skills(
    home: Path,
    slug: str,
    pid: int | None,
    mounted_drivers: set[str] | None = None,
) -> None:
    """Build `home/memory/skills/` as a per-PAI filtered view.

    Each skill's `visible_to:` is consulted; a public skill (no field) is
    linked for everyone, a restricted skill is only linked when the PAI's
    slug or pid is in the list. Re-runs are cheap — we wipe symlinks under
    the dir and rebuild from `/usr/lib/skills/`.
    """
    skills_root = paths.usr_lib_skills()
    target_root = home / "memory" / "skills"

    # Legacy: `memory/skills` used to be a single symlink to /usr/lib/skills.
    # Replace it with a real directory so we can populate per-skill symlinks.
    if target_root.is_symlink():
        target_root.unlink()
    target_root.mkdir(parents=True, exist_ok=True)

    # Wipe the existing symlink tree so removed/now-hidden skills disappear.
    # Real files/dirs (someone dropped a note in here) are left alone.
    if target_root.exists():
        for topic_dir in list(target_root.iterdir()):
            if not topic_dir.is_dir() or topic_dir.is_symlink():
                if topic_dir.is_symlink():
                    topic_dir.unlink()
                continue
            for entry in list(topic_dir.iterdir()):
                if entry.is_symlink():
                    entry.unlink()
            try:
                topic_dir.rmdir()
            except OSError:
                # Non-empty (user added something) — leave it.
                pass

    if skills_root.exists():
        for topic_dir in sorted(skills_root.iterdir()):
            if not topic_dir.is_dir() or topic_dir.name.startswith("."):
                continue
            # Flat skill (SKILL.md directly under topic dir, no nested topic).
            if (topic_dir / "SKILL.md").exists():
                if _skills_filter.is_visible(
                    topic_dir / "SKILL.md", slug, pid or 0, mounted_drivers
                ):
                    link = target_root / topic_dir.name
                    if not link.exists():
                        link.symlink_to(topic_dir.resolve(), target_is_directory=True)
                continue
            for skill_dir in sorted(topic_dir.iterdir()):
                if not skill_dir.is_dir() or skill_dir.name.startswith("."):
                    continue
                skill_md = skill_dir / "SKILL.md"
                if not skill_md.exists():
                    continue
                if not _skills_filter.is_visible(skill_md, slug, pid or 0, mounted_drivers):
                    continue
                link = target_root / topic_dir.name / skill_dir.name
                link.parent.mkdir(parents=True, exist_ok=True)
                if link.is_symlink() or link.exists():
                    continue
                link.symlink_to(skill_dir.resolve(), target_is_directory=True)

    # Overlay writable self-written skills over the read-only baseline. Shared
    # skills (every PAI) are applied first, then this PAI's private skills, so a
    # PAI's own adaptation wins over a fleet-shared one of the same name. Both
    # win over a baseline skill of the same top-level name (the overlay IS the
    # adaptation) — see the plan. Overlay skills are flat: `<root>/<name>/SKILL.md`.
    _overlay_skills(target_root, paths.var_lib_skills(), slug, pid, mounted_drivers)
    _overlay_skills(
        target_root,
        paths.var_lib_instance_skills(slug),
        slug,
        pid,
        mounted_drivers,
    )


def _overlay_skills(
    target_root: Path,
    overlay_root: Path,
    slug: str,
    pid: int | None,
    mounted_drivers: set[str] | None,
) -> None:
    """Link each flat `<overlay_root>/<name>/SKILL.md` into `target_root/<name>`,
    overriding any baseline entry of the same name (the overlay wins)."""
    if not overlay_root.exists():
        return
    for skill_dir in sorted(overlay_root.iterdir()):
        if not skill_dir.is_dir() or skill_dir.name.startswith("."):
            continue
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.exists():
            continue
        if not _skills_filter.is_visible(skill_md, slug, pid or 0, mounted_drivers):
            continue
        link = target_root / skill_dir.name
        # Overlay wins: drop a colliding baseline symlink (or empty dir) first.
        if link.is_symlink():
            link.unlink()
        elif link.is_dir():
            try:
                link.rmdir()
            except OSError:
                # Non-empty real dir (e.g. a populated baseline topic) — leave it.
                continue
        elif link.exists():
            continue
        link.symlink_to(skill_dir.resolve(), target_is_directory=True)


_PRIVATE_MEMORY_INDEX_HEADER = (
    "<!-- Private MEMORY index. Owned by librarian; use `memorize --private` instead of editing directly. -->\n"
)


def _seed_instance(instance: Path) -> None:
    """Ensure the instance state dirs exist. Never overwrites."""
    instance.mkdir(parents=True, exist_ok=True)
    for sub in (
        "inbox",
        "workspace",
        "memory/private",
        "memory/private/journal",
        "memory/private/topics",
        "skills",
    ):
        (instance / sub).mkdir(parents=True, exist_ok=True)
    index = instance / "memory" / "private" / "MEMORY.md"
    if not index.exists():
        index.write_text(_PRIVATE_MEMORY_INDEX_HEADER)


def home_for(slug: str) -> Path:
    """The pid-1 PAI lives at /root/; everyone else at /home/<slug>/."""
    return paths.root_home() if slug == "root" else paths.home_pai(slug)


def _parse_home_links(pkg_path: Path) -> tuple[tuple[str, Path], ...]:
    """Read `home.links` from any bundle's `package.yaml`.

    Each entry becomes a `(link, target)` tuple suitable for `extra_links`.
    Targets are relative to PAI_ROOT and must stay within it; links may not
    collide with the kernel-seeded universals.
    """
    if not pkg_path.exists():
        return ()
    with pkg_path.open() as f:
        data = yaml.safe_load(f) or {}
    home_block = data.get("home") or {}
    raw_links = home_block.get("links") or []
    if not isinstance(raw_links, list):
        raise ValueError(f"{pkg_path}: home.links must be a list")
    root_resolved = paths.PAI_ROOT.resolve()
    out: list[tuple[str, Path]] = []
    seen: set[str] = set()
    for entry in raw_links:
        if not isinstance(entry, dict):
            raise ValueError(f"{pkg_path}: home.links items must be mappings")
        link = entry.get("link")
        target = entry.get("target")
        if not isinstance(link, str) or not link:
            raise ValueError(f"{pkg_path}: home.links entry missing string `link`")
        if not isinstance(target, str) or not target:
            raise ValueError(f"{pkg_path}: home.links entry missing string `target`")
        if link.startswith("/") or link.startswith(".."):
            raise ValueError(f"{pkg_path}: invalid link {link!r}")
        top = link.split("/", 1)[0]
        if top in RESERVED_HOME_LINKS:
            raise ValueError(
                f"{pkg_path}: home.link {link!r} collides with reserved home entry {top!r}"
            )
        if link in seen:
            raise ValueError(f"{pkg_path}: duplicate home.link {link!r}")
        seen.add(link)
        target_path = Path(target)
        if target_path.is_absolute():
            raise ValueError(
                f"{pkg_path}: target {target!r} must be relative to PAI_ROOT"
            )
        resolved = (paths.PAI_ROOT / target_path).resolve()
        try:
            resolved.relative_to(root_resolved)
        except ValueError as e:
            raise ValueError(
                f"{pkg_path}: target {target!r} escapes PAI_ROOT"
            ) from e
        out.append((link, target_path))
    return tuple(out)


def _installed_driver_names() -> list[str]:
    drivers_root = paths.usr_lib_drivers()
    if not drivers_root.exists():
        return []
    out: list[str] = []
    for entry in sorted(drivers_root.iterdir()):
        if entry.name.startswith("."):
            continue
        if (entry / "package.yaml").is_file():
            out.append(entry.name)
    return out


def _driver_dep_name(dep: object) -> str | None:
    if not isinstance(dep, str) or not dep:
        return None
    if dep.startswith("drivers/"):
        return dep.split("/", 1)[1]
    if dep.startswith("driver/"):
        return dep.split("/", 1)[1]
    if "/" in dep:
        return None
    return dep


def _bundle_package_path(slug: str, package: str | None) -> Path | None:
    if package:
        return paths.usr_lib_pais() / package / "package.yaml"
    from . import processes  # local import: stitch loaded at boot

    try:
        spec = processes.read_spec(slug)
    except Exception:
        return None
    spec_package = spec.get("package")
    if not isinstance(spec_package, str) or not spec_package:
        return None
    if "parent" in spec:
        return paths.usr_lib_subagents() / spec_package / "package.yaml"
    return paths.usr_lib_pais() / spec_package / "package.yaml"


def mounted_drivers_for(slug: str) -> set[str]:
    """Return the set of driver names this PAI mounts.

    Rules:
    - A fallback PAI (`fallback: true` in /etc/config.yaml) mounts every
      installed driver — it must be able to handle any unrouted event.
    - A bundled PAI mounts the drivers listed in its bundle `deps:` that
      are installed locally.
    - A bundled subagent mounts the drivers listed in its subagent bundle
      `deps:` that are installed locally.
    - A bundleless, non-fallback PAI (e.g. `root`) mounts no drivers.
    """
    from . import config

    installed = set(_installed_driver_names())
    if not installed:
        return set()
    try:
        if config.is_fallback(slug):
            return set(installed)
    except Exception:
        pass
    package: str | None = None
    try:
        package = config.package_for(slug)
    except Exception:
        package = None
    if not package:
        pkg_path = _bundle_package_path(slug, None)
    else:
        pkg_path = _bundle_package_path(slug, package)
    if pkg_path is None:
        return set()
    if not pkg_path.exists():
        return set()
    try:
        with pkg_path.open() as f:
            data = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        return set()
    deps = data.get("deps") or []
    if not isinstance(deps, list):
        return set()
    return {
        driver
        for dep in deps
        if (driver := _driver_dep_name(dep)) is not None and driver in installed
    }


def _driver_home_links(drivers: set[str]) -> tuple[tuple[str, Path], ...]:
    """Union of `home.links` declared by each mounted driver's bundle."""
    out: list[tuple[str, Path]] = []
    seen: set[str] = set()
    for name in sorted(drivers):
        pkg = paths.usr_lib_drivers() / name / "package.yaml"
        for link, target in _parse_home_links(pkg):
            if link in seen:
                continue
            seen.add(link)
            out.append((link, target))
    return tuple(out)


def _resolve_extras(slug: str, mounted_drivers: set[str]) -> tuple[tuple[str, Path], ...]:
    """Find the slug's home extras. Bundle-declared `home.links` come first,
    then driver `home.links` for every mounted driver. Bundleless PAIs fall
    back to the per-slug seed map (root/pai keep `communication`)."""
    from . import config  # local import: stitch is imported during boot init

    package: str | None = None
    try:
        package = config.package_for(slug)
    except Exception:
        package = None
    bundle_extras: tuple[tuple[str, Path], ...]
    if package:
        bundle_extras = _parse_home_links(paths.usr_lib_pais() / package / "package.yaml")
    else:
        bundle_extras = _BUNDLELESS_SEEDS.get(slug, ())
        if not bundle_extras:
            # Subagents (bundleless kind:pai procs with a parent) need the
            # communication view so they can send-message back. Read the
            # spec directly — they aren't in /etc/config.yaml.
            from . import processes  # local import: stitch loaded at boot
            try:
                spec = processes.read_spec(slug)
            except Exception:
                spec = {}
            if "parent" in spec:
                bundle_extras = (_COMMUNICATION_LINK,)
    driver_extras = _driver_home_links(mounted_drivers)
    seen: set[str] = {link for link, _ in bundle_extras}
    extras = list(bundle_extras)
    for link, target in driver_extras:
        if link in seen:
            raise ValueError(
                f"home.link {link!r} declared by driver collides with bundle/seed for {slug!r}"
            )
        seen.add(link)
        extras.append((link, target))
    return tuple(extras)


def stitch_home(slug: str) -> Path:
    """Build (or heal) the home tree for `slug`. Returns the home path."""
    from . import processes  # local import to avoid cycle at module load

    instance = paths.var_lib_instance(slug)
    home = home_for(slug)
    _seed_instance(instance)
    home.mkdir(parents=True, exist_ok=True)
    drivers = mounted_drivers_for(slug)
    extra = _resolve_extras(slug, drivers)
    _stitch_links(home, instance, extra)
    pid: int | None
    try:
        pid = processes.read_pai_pid(slug)
    except Exception:
        pid = None
    _stitch_skills(home, slug, pid, drivers)
    return home
