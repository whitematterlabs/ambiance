"""Phases 5–6: start kernelPAI first, then the fleet.

Today this is largely a no-op wrapper because reconcile already spawns
proc entries and `proc-watcher` resumes the running set in supervise.
The phase exists so the boot sequence has an explicit hook: when the
proc-layer follow-up lands and process spawning becomes lifecycle-
aware, the kernelPAI-first ordering will be enforced here.
"""
from __future__ import annotations


def run() -> None:
    # Reserved for the proc-layer plan. Current proc semantics are
    # spec-on-disk; resume happens inside supervise.entry().
    print("[boot] start: kernelPAI + fleet (deferred to supervise loop)", flush=True)
