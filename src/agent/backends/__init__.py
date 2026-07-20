"""Provider backends — one module per wire format, no translation layer.

`for_wire()` is the only routing: a provider's `wire` field in
base.PROVIDERS names the module that owns its conversation end to end.
"""

from __future__ import annotations

from types import ModuleType

from . import anthropic, base, openai

_BY_WIRE: dict[str, ModuleType] = {
    "anthropic": anthropic,
    "openai": openai,
}


def for_wire(wire: str) -> ModuleType:
    try:
        return _BY_WIRE[wire]
    except KeyError:
        raise ValueError(f"unknown wire format: {wire!r}") from None
