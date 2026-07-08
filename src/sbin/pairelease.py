#!/usr/bin/env python
"""pairelease — build (and optionally publish) a PAI release tarball.

Owner/dev-box tool. The end-user install path is a `curl … | sh` one-liner
(see install.sh) that downloads a prebuilt tarball — no uv, Node, or git on
the target machine. This tool produces that tarball.

A release is a single arch-neutral artifact: tracked source + `uv.lock` +
`.python-version`, with the freshly built web `dist/` overlaid in (it is
git-ignored, so `git archive` omits it and we copy it explicitly). The target
machine runs `uv sync` against the lockfile to pull prebuilt wheels — no
compiler — and `paifs-init` to provision the FHS.

The version string carries a build counter on publish. The base semver lives in
pyproject.toml (pinned, e.g. `0.1.0`); each --publish reads the currently
published `version.txt`, increments its `+build.N` suffix, and stamps the new
build — so the rolling `latest` release is distinguishable build-to-build even
though the semver never moves. The GitHub *tag* stays `v<base>` (clobbered in
place); only `version.txt` carries the counter. A plain build (no --publish)
keeps the bare base semver.

Steps:
  0. Pre-flight: every dual-homed tool (src/{bin,sbin}/<name>.py with an
     installable copy in ~/Projects/pairegistry) must be byte-identical with
     its registry copy — hard-fails otherwise (bypass: --skip-drift-check).
  1. Read the base version from pyproject.toml [project].version. On --publish,
     append `+build.<N>` where N is one past the published version.txt.
  2. Build the web surface (`pnpm install && pnpm build`).
  3. Stage tracked files via `git archive HEAD`, overlay the built `dist/`.
  4. Prune dev-only trees (tests/, development_docs/, docs/).
  5. Emit dist/pai-<ver>.tar.gz, a stable dist/pai.tar.gz, dist/version.txt,
     and dist/pai.tar.gz.sha256.
  6. With --publish: create/update the GitHub release `v<base>`.

Dev-box prereqs (acceptable — this is a build tool): pnpm, git, and, for
--publish, gh.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

from boot.paths import REPO_ROOT

# Web source dir whose built `dist/` must be overlaid into the staged tree.
WEB_DIR_REL = Path("src") / "usr" / "libexec" / "web"

# Where consumers (install.sh, `pai update`) fetch release assets from. We read
# the current build counter back from this same `version.txt` so the number we
# publish is always one past what end machines last saw.
DEFAULT_RELEASE_BASE = "https://github.com/whitematterlabs/pai/releases/latest/download"

# Matches the build-counter suffix in a published version string, e.g. the
# `+build.42` in `0.1.0+build.42`.
_BUILD_RE = re.compile(r"\+build\.(\d+)")

# Dev-only trees pruned from the staged tree before tarring. Note we keep
# src/usr/share/doc (runtime PAI docs) — only top-level dev dirs are dropped.
PRUNE_DIRS: tuple[str, ...] = ("tests", "development_docs", "docs")

# The canonical package registry (see CLAUDE.md: pairegistry is upstream).
# Dual-homed tools have dev source at src/{bin,sbin}/<name>.py and an
# installable copy at <registry>/{bin,sbin}/<pkg>/<file>.py; the two must be
# byte-identical at release time. Overridable for tests / other checkouts.
DEFAULT_REGISTRY_ROOT = Path.home() / "Projects" / "pairegistry"


def _registry_root() -> Path:
    return Path(os.environ.get("PAI_REGISTRY_ROOT", str(DEFAULT_REGISTRY_ROOT)))


def _name_variants(name: str) -> list[str]:
    """Registry package dirs (and sometimes files) hyphenate underscores,
    e.g. src/bin/send_message.py ↔ bin/send-message/send_message.py."""
    return list(dict.fromkeys([name, name.replace("_", "-")]))


def _registry_copy(registry: Path, kind: str, name: str) -> Path | None:
    """Locate the registry's installable copy of a src/<kind>/<name>.py tool.

    Checks the matching registry kind first, then the other one (tools have
    historically moved between bin/ and sbin/). Both the package dir and the
    file inside it may use either underscores or hyphens."""
    kinds = (kind, "sbin" if kind == "bin" else "bin")
    for k in kinds:
        for pkg in _name_variants(name):
            for fname in _name_variants(name):
                cand = registry / k / pkg / f"{fname}.py"
                if cand.is_file():
                    return cand
    return None


def discover_dual_homed(repo: Path, registry: Path) -> list[tuple[Path, Path]]:
    """Empirically build the dual-homed mapping: every src/{bin,sbin}/*.py that
    has an installable copy in the registry. Discovered, not hardcoded, so a
    newly-registered tool is covered the moment its registry package exists."""
    pairs: list[tuple[Path, Path]] = []
    for kind in ("bin", "sbin"):
        src_dir = repo / "src" / kind
        if not src_dir.is_dir():
            continue
        for src in sorted(src_dir.glob("*.py")):
            if src.stem == "__init__":
                continue
            reg = _registry_copy(registry, kind, src.stem)
            if reg is not None:
                pairs.append((src, reg))
    return pairs


def find_dual_homed_drift(repo: Path, registry: Path) -> list[tuple[Path, Path]]:
    """Return the (repo_copy, registry_copy) pairs that are not byte-identical."""
    return [
        (src, reg)
        for src, reg in discover_dual_homed(repo, registry)
        if src.read_bytes() != reg.read_bytes()
    ]


def check_dual_homed_drift(repo: Path, registry: Path) -> None:
    """Release pre-flight: hard-fail if any dual-homed tool has drifted from
    its registry copy. The registry is upstream (CLAUDE.md) and must never be
    behind a release."""
    if not registry.is_dir():
        print(
            f"pairelease: WARNING — package registry not found at {registry}; "
            "skipping dual-homed drift check (set PAI_REGISTRY_ROOT to point at it)",
            file=sys.stderr,
        )
        return
    drifted = find_dual_homed_drift(repo, registry)
    if not drifted:
        return
    lines = [
        "pairelease: dual-homed drift — these tools differ from their registry copies:"
    ]
    for src, reg in drifted:
        lines.append(f"  {src.stem}:")
        lines.append(f"    repo:     {src}")
        lines.append(f"    registry: {reg}")
    lines.append(
        "The registry is upstream and must never be behind (CLAUDE.md). Diff each"
    )
    lines.append(
        "pair, copy the newer side over the stale one, then rerun. To bypass"
    )
    lines.append("(you almost never should): --skip-drift-check.")
    sys.exit("\n".join(lines))


def read_version(repo: Path) -> str:
    with (repo / "pyproject.toml").open("rb") as f:
        data = tomllib.load(f)
    version = (data.get("project") or {}).get("version")
    if not version or not isinstance(version, str):
        sys.exit("pairelease: pyproject.toml [project].version missing")
    return version


def _release_base() -> str:
    return os.environ.get("PAI_RELEASE_BASE", DEFAULT_RELEASE_BASE)


def parse_build_number(version_text: str) -> int:
    """Extract the build counter from a published version string. Returns 0 when
    no `+build.N` suffix is present (a base-semver or hand-cut release)."""
    m = _BUILD_RE.search(version_text)
    return int(m.group(1)) if m else 0


def next_build_number(base: str) -> int:
    """The build counter to stamp on this publish: one past the currently
    published version.txt.

    A missing release (HTTP 404) means this is the first publish → start at 1.
    Any *other* fetch failure aborts the publish: defaulting to 1 on a transient
    network blip would silently regress the counter and clobber the release with
    a lower build number than end machines already have."""
    url = f"{base}/version.txt"
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            text = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return 1
        sys.exit(
            f"pairelease: could not read current build counter from {url} "
            f"(HTTP {e.code}); aborting to avoid regressing the counter"
        )
    except (urllib.error.URLError, OSError, ValueError) as e:
        sys.exit(
            f"pairelease: could not read current build counter from {url} "
            f"({e}); aborting to avoid regressing the counter"
        )
    return parse_build_number(text) + 1


def _run(cmd: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> None:
    try:
        subprocess.run(cmd, cwd=str(cwd), check=True, env=env)
    except FileNotFoundError as e:
        sys.exit(f"pairelease: `{cmd[0]}` not found on PATH")
    except subprocess.CalledProcessError as e:
        sys.exit(f"pairelease: command failed ({e.returncode}): {' '.join(cmd)}")


def build_web(repo: Path, version: str) -> None:
    """Build the web surface so its (git-ignored) dist/ can be shipped.

    `version` is baked into the bundle (VITE_PAI_BUILD → import.meta.env) so a
    loaded tab can tell exactly which release its JS came from and reload
    itself when the console server moves to a newer one. It must match the
    opt/pai/<ver> dir name `pai update` will extract this tarball into — i.e.
    the version.txt string."""
    web_dir = repo / WEB_DIR_REL
    if not web_dir.is_dir():
        sys.exit(f"pairelease: web dir not found: {web_dir}")
    print("==> web frontend (pnpm)")
    _run(["pnpm", "install"], cwd=web_dir)
    _run(
        ["pnpm", "build"],
        cwd=web_dir,
        env={**os.environ, "VITE_PAI_BUILD": version},
    )
    dist = web_dir / "dist"
    if not dist.is_dir() or not any(dist.iterdir()):
        sys.exit(f"pairelease: web build produced no dist/ at {dist}")


def stage(repo: Path, staging: Path) -> None:
    """Populate `staging` with the tracked tree (git archive) + the built dist.

    `git archive HEAD` emits only tracked files — no node_modules, .venv, or
    .git — so the tarball is lean by construction. The web dist/ is git-ignored
    and therefore absent from the archive; we copy it in afterward.
    """
    staging.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(suffix=".tar", delete=False) as tmp:
        archive = Path(tmp.name)
    try:
        _run(["git", "archive", "--format=tar", "-o", str(archive), "HEAD"], cwd=repo)
        with tarfile.open(archive) as tf:
            members = tf.getmembers()
            if not members:
                sys.exit("pairelease: `git archive HEAD` produced an empty tree")
            tf.extractall(staging, filter="tar")
    finally:
        archive.unlink(missing_ok=True)

    # Drop machine-specific absolute symlinks (e.g. src/prompts/*.md point into
    # the dev's ~/.pai). They'd ship dangling; the target installs the real
    # prompts via paiman's kernel-essentials seed during paifs-init.
    stripped = strip_nonportable_symlinks(staging)
    for rel in stripped:
        print(f"    stripped non-portable symlink: {rel}")

    # Overlay the freshly built web dist/ (git-ignored → not in the archive).
    src_dist = repo / WEB_DIR_REL / "dist"
    if not src_dist.is_dir():
        sys.exit(f"pairelease: built dist/ missing at {src_dist}; run build first")
    dest_dist = staging / WEB_DIR_REL / "dist"
    if dest_dist.exists():
        shutil.rmtree(dest_dist)
    dest_dist.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src_dist, dest_dist)

    prune_staging(staging)


def strip_nonportable_symlinks(staging: Path) -> list[str]:
    """Remove symlinks with absolute targets from the staged tree. Such links
    encode the dev machine's paths and would dangle on any other machine.
    Returns the staging-relative paths removed."""
    removed: list[str] = []
    for path in sorted(staging.rglob("*")):
        if path.is_symlink() and Path(path.readlink()).is_absolute():
            path.unlink()
            removed.append(str(path.relative_to(staging)))
    return removed


def prune_staging(staging: Path) -> list[str]:
    """Remove dev-only top-level trees. Returns the names actually removed."""
    removed: list[str] = []
    for name in PRUNE_DIRS:
        target = staging / name
        if target.is_dir():
            shutil.rmtree(target)
            removed.append(name)
        elif target.exists():
            target.unlink()
            removed.append(name)
    return removed


def make_tarball(staging: Path, out: Path) -> None:
    """Tar the *contents* of staging (no wrapping dir) so extraction lands
    src/, pyproject.toml, … directly under the destination version dir."""
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    with tarfile.open(out, "w:gz") as tf:
        for entry in sorted(staging.iterdir()):
            tf.add(entry, arcname=entry.name)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def publish(base_version: str, full_version: str, dist_dir: Path) -> None:
    """Create or update the rolling GitHub release with the release assets.

    The tag stays pinned to the base semver (`v<base_version>`) so the release
    is clobbered in place and `releases/latest/download` keeps resolving to it —
    the build counter rolls inside `version.txt`, not in the tag. `full_version`
    (`<base>+build.<N>`) only labels the release notes."""
    tag = f"v{base_version}"
    assets = [
        str(dist_dir / "pai.tar.gz"),
        str(dist_dir / "pai.tar.gz.sha256"),
        str(dist_dir / "version.txt"),
    ]
    exists = (
        subprocess.run(
            ["gh", "release", "view", tag],
            capture_output=True,
            text=True,
        ).returncode
        == 0
    )
    if exists:
        print(f"==> updating existing release {tag}")
        _run_gh(["gh", "release", "upload", tag, *assets, "--clobber"])
    else:
        print(f"==> creating release {tag}")
        _run_gh(
            [
                "gh",
                "release",
                "create",
                tag,
                *assets,
                "--title",
                tag,
                "--notes",
                f"PAI {full_version}",
            ]
        )


def _run_gh(cmd: list[str]) -> None:
    try:
        subprocess.run(cmd, check=True)
    except FileNotFoundError:
        sys.exit("pairelease: `gh` not found — install GitHub CLI to --publish")
    except subprocess.CalledProcessError as e:
        sys.exit(f"pairelease: gh failed ({e.returncode}): {' '.join(cmd)}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="pairelease", description=__doc__)
    ap.add_argument(
        "--publish",
        action="store_true",
        help="create/update the GitHub release v<ver> with the built assets",
    )
    ap.add_argument(
        "--no-web",
        action="store_true",
        help="skip the pnpm build (reuse an already-built dist/)",
    )
    ap.add_argument(
        "--skip-drift-check",
        action="store_true",
        help="bypass the dual-homed registry drift pre-flight (escape hatch)",
    )
    args = ap.parse_args(argv)

    repo = REPO_ROOT

    # Pre-flight, before any build/publish work: every dual-homed tool
    # (src/{bin,sbin} ↔ pairegistry) must be byte-identical with its registry
    # copy — the registry is upstream and a release must never ship ahead of it.
    if not args.skip_drift_check:
        check_dual_homed_drift(repo, _registry_root())

    base_version = read_version(repo)

    # The build counter only advances on --publish (its source of truth is the
    # published version.txt). A plain build keeps the bare base semver so local
    # artifacts don't carry a counter they never reserved.
    if args.publish:
        build = next_build_number(_release_base())
        version = f"{base_version}+build.{build}"
    else:
        version = base_version
    print(f"==> building PAI {version}")

    if not args.no_web:
        build_web(repo, version)

    dist_dir = repo / "dist"
    with tempfile.TemporaryDirectory(prefix="pairelease-") as tmp:
        staging = Path(tmp) / "stage"
        stage(repo, staging)
        versioned = dist_dir / f"pai-{version}.tar.gz"
        make_tarball(staging, versioned)

    stable = dist_dir / "pai.tar.gz"
    shutil.copy2(versioned, stable)
    (dist_dir / "version.txt").write_text(f"{version}\n")
    digest = _sha256(stable)
    (dist_dir / "pai.tar.gz.sha256").write_text(f"{digest}  pai.tar.gz\n")

    print(f"    {versioned}")
    print(f"    {stable}")
    print(f"    sha256: {digest}")

    if args.publish:
        publish(base_version, version, dist_dir)

    return 0


if __name__ == "__main__":
    sys.exit(main())
