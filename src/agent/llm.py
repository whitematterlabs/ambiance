"""The inference surface — provider-agnostic dispatch to wire backends.

Callers (turn.py) see one contract: run_turn(system, user, history, …)
→ (reply, messages). Which wire the conversation speaks — and therefore
what shape `messages` takes on disk — is decided by the provider's
entry in backends.base.PROVIDERS. Backends are siblings, never
translated between; switching a member across wire families is the turn
engine's compact-and-reseed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from . import backends
from .backends.base import (  # noqa: F401 — public re-exports
    DEFAULT_PROVIDER,
    PROVIDERS,
    ProviderSpec,
    TurnCancelled,
    resolve,
    wire_for,
)


async def run_turn(
    system: str,
    user: str,
    history: Optional[list[dict]] = None,
    *,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    state_dir: Optional[Path] = None,
    home: Optional[Path] = None,
    drain: Optional[Callable[[], list[str]]] = None,
) -> tuple[str, list[dict]]:
    key, spec, model = resolve(provider, model)
    backend = backends.for_wire(spec.wire)
    return await backend.run_turn(
        key,
        spec,
        model,
        system,
        user,
        history,
        state_dir=state_dir,
        home=home,
        drain=drain,
    )
