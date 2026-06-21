"""paisetup — interactive registry installer + PAI configurator.

Runs at the end of install.sh, after paifs-init. Lets the user pick extra
packages from the registry (drivers, skills, PAI bundles, subagents) via a
menuconfig-style curses picker, installs them, and for any selected PAI
bundle hands off to paiadd's interactive wizard.
"""

from __future__ import annotations

import sys
from pathlib import Path

from bin import paiman, paiadd

from . import picker
from .inventory import Item, discover


def _tty_available() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _install_arg(it: Item) -> str:
    """Pick the argument to hand `paiman install` for a discovered item.

    Prefer the on-disk source path when it's still live — that's the case for a
    local registry (PAIMAN_REGISTRY=<dir>), and it disambiguates names shared
    across kinds (e.g. bin/browse vs subagents/browse). For a URL-cloned
    registry the source path points into a TemporaryDirectory that discover()
    has already deleted, so fall back to the typed registry ref
    (`skills/<topic>/<name>`), which paiman.lookup resolves directly. The bare
    name is a last resort: paiman's bare-name lookup only resolves one-level
    kinds like drivers/<name>, never topic-nested skills.
    """
    if it.source and Path(it.source).is_dir():
        return it.source
    if it.ref:
        return it.ref
    return it.name


def _emit_catalog_json() -> int:
    """Emit the owner-facing capability catalog as one JSON line on stdout.

    Backs PAI.app's first-run capability picker — the GUI twin of the curses
    checklist. PAI bundles are intentionally excluded: configuring an instance
    is paiadd's job, and paiadd is PAI's own tool, not owner-facing. The git
    clone in discover() writes progress to stderr, so stdout stays clean JSON."""
    import json

    try:
        groups = discover()
    except SystemExit as e:
        print(json.dumps({"error": str(e)}))
        return 1
    payload = {
        "schema": 1,
        "auto_checked": sorted(picker.AUTO_CHECKED),
        "auto_checked_refs": picker.auto_checked_refs(),
        "groups": {
            kind: [
                {
                    "name": it.name,
                    "description": it.description,
                    "installed": it.installed,
                    "ref": it.ref or it.name,
                    "default_checked": it.installed or picker.is_auto_checked(kind, it),
                }
                for it in groups.get(kind, [])
            ]
            for kind in picker.VISIBLE_KINDS
            if kind != "pai"
        },
    }
    print(json.dumps(payload))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if "--json" in args:
        return _emit_catalog_json()
    if not _tty_available():
        print("paisetup: non-interactive shell — skipping. Run `paisetup` later to add packages.")
        return 0

    print("Discovering registry packages...")
    try:
        groups = discover()
    except SystemExit as e:
        print(f"paisetup: {e}", file=sys.stderr)
        return 1

    selected = picker.run(groups)
    if selected is None:
        print("paisetup: cancelled.")
        return 0

    install_order = ("driver", "skill", "subagent", "pai")
    total = sum(len(selected.get(k, [])) for k in install_order)
    if total == 0:
        print("paisetup: nothing to install.")
        return 0

    print(f"\nInstalling {total} package(s)...")
    failures: list[str] = []
    # The picker hands back bare names; map each (kind, name) back to its
    # discovered Item so _install_arg can recover an argument paiman can
    # actually resolve (live source path, else typed ref — see _install_arg).
    items_by_key: dict[tuple[str, str], Item] = {}
    for kind, items in groups.items():
        for it in items:
            items_by_key[(kind, it.name)] = it
    for kind in install_order:
        for name in selected.get(kind, []):
            it = items_by_key.get((kind, name))
            arg = _install_arg(it) if it is not None else name
            print(f"\n--- paiman install {arg} ---")
            try:
                # --no-reload: each install would otherwise emit its own
                # kernel:reload_config, and a full reconcile (drain every PAI
                # lock + re-stitch all homes) per package serializes into a
                # storm. Suppress here; emit one reload after the batch below.
                rc = paiman.main(["install", "--no-reload", arg])
            except SystemExit as e:
                rc = e.code if isinstance(e.code, int) else 1
            except Exception as e:
                print(f"paisetup: install of {name!r} failed: {e}", file=sys.stderr)
                rc = 1
            if rc != 0:
                failures.append(name)

    installed_any = total - len(failures) > 0
    if installed_any:
        # One reconcile for the whole batch: re-stitches homes so newly
        # installed skills/prompts surface, and re-discovers drivers so they
        # start, all without a kernel reboot. paiadd (below) emits its own
        # reload, but instances may be configured even when no packages land,
        # so emit unconditionally here when anything installed.
        try:
            from boot import processes as _processes
            _processes.emit_event({"kind": "kernel:reload_config",
                                   "source": "paisetup", "action": "install"})
        except Exception as e:
            print(f"paisetup: warning — could not emit kernel:reload_config: {e}",
                  file=sys.stderr)

    pai_bundles = selected.get("pai", [])
    configured: list[str] = []
    for bundle in pai_bundles:
        if bundle in failures:
            continue
        print(f"\n--- paiadd {bundle} (configure instance) ---")
        try:
            rc = paiadd.main([bundle])
        except SystemExit as e:
            rc = e.code if isinstance(e.code, int) else 1
        except KeyboardInterrupt:
            print(f"paisetup: skipped configuring {bundle}.")
            continue
        if rc == 0:
            configured.append(bundle)

    print()
    print(f"paisetup: installed {total - len(failures)} package(s), "
          f"configured {len(configured)} PAI instance(s).")
    if failures:
        print(f"paisetup: failed: {', '.join(failures)}", file=sys.stderr)
        return 1
    return 0
