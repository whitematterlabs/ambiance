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
from .inventory import discover


def _tty_available() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def main(argv: list[str] | None = None) -> int:
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
    # Build a quick (kind, name) → on-disk source lookup so paiman gets
    # an unambiguous path when a name appears under multiple kinds (e.g.
    # bin/browse vs subagents/browse). Falls back to the bare name if the
    # discovered source path is no longer valid (e.g. tempdir cleanup
    # after a URL-cloned registry).
    sources: dict[tuple[str, str], str] = {}
    for kind, items in groups.items():
        for it in items:
            if it.source:
                sources[(kind, it.name)] = it.source
    for kind in install_order:
        for name in selected.get(kind, []):
            src = sources.get((kind, name))
            arg = src if src and Path(src).is_dir() else name
            print(f"\n--- paiman install {arg} ---")
            try:
                rc = paiman.main(["install", arg])
            except SystemExit as e:
                rc = e.code if isinstance(e.code, int) else 1
            except Exception as e:
                print(f"paisetup: install of {name!r} failed: {e}", file=sys.stderr)
                rc = 1
            if rc != 0:
                failures.append(name)

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
