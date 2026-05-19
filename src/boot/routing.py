"""Event-kind → target PAI pid resolution.

Shared between the live kernel loop (boot.main) and boot-time backfill
detection (boot.phases.backfill). The function reads /proc/<pai>/spec.yaml
for every kind:pai proc and matches `wake_on` globs against the event kind.
"""

from __future__ import annotations

import fnmatch

from . import processes as P


def route_to_pids(
    event_kind: str,
    fallback_pid: int = 1,
    target_pid: int | None = None,
) -> list[int]:
    """Every running PAI that should be nudged for `event_kind`, by pid.

    If `target_pid` is set, deliver only to that pid (when it's a running
    kind:pai) and skip wake_on/fallback entirely. This is the channel used
    by drivers that own per-PAI session state and need to address a
    specific PAI rather than broadcast by kind.

    Otherwise two-tier:
      1. Every PAI whose `wake_on` glob matches → nudged (fan-out).
      2. If zero PAIs matched, every PAI with `fallback: true` → nudged.
      3. If still zero, [fallback_pid] (pid 1 = kernel_manager) so the
         event always lands somewhere.
    """
    if target_pid is not None:
        for slug, spec in P._iter_pai_specs():
            if spec.get("pid") != target_pid:
                continue
            try:
                if P.read_status(slug) != "running":
                    return []
            except P.ProcessNotFound:
                return []
            return [int(target_pid)]
        return []

    matched: list[int] = []
    fallbacks: list[int] = []
    for slug, spec in P._iter_pai_specs():
        try:
            if P.read_status(slug) != "running":
                continue
        except P.ProcessNotFound:
            continue
        pid = spec.get("pid")
        if not isinstance(pid, int):
            continue
        wake_on = spec.get("wake_on") or []
        if isinstance(wake_on, list) and any(
            fnmatch.fnmatchcase(event_kind, pat) for pat in wake_on
        ):
            matched.append(pid)
        elif spec.get("fallback") is True:
            fallbacks.append(pid)
    chosen = matched or fallbacks or [fallback_pid]
    chosen.sort()
    return chosen
