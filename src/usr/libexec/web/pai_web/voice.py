"""Engine-agnostic voice dispatch for the web surface.

The web backend's `/api/stt` and `/api/tts` must not name an engine. This module
resolves a capability (`stt` / `tts`) to whichever voice **provider package** is
installed and configured, then hands back the imported `provider.py` module so
`actions.py` can call `provider.transcribe(...)` / `provider.synthesize(...)`.

Provider packages declare themselves in `package.yaml`:

    provides: [stt, tts]        # or a subset
    voice_mode: local | cloud   # default-selection preference

and ship a `provider.py` with the contract:

    transcribe(audio, *, content_type, filename, language=None, prompt=None) -> str
    synthesize(text, *, voice_id=None, speed=None) -> (bytes, mime)

Selection order for a capability:
  1. `/etc/config.yaml` → `voice.<capability>`, else `voice.provider` (explicit pin).
  2. Otherwise installed packages that declare the capability, **local before
     cloud** (so on-device whisper/`say` win when present).
A package whose native deps don't import (ImportError) is skipped, so a configured
local provider gracefully yields to cloud when its runtime isn't provisioned.
"""

from __future__ import annotations

import importlib
import threading

import yaml

from boot.paths import PAI_ROOT
from boot.config import CONFIG_PATH

_CAPABILITIES = ("stt", "tts")

# Discovery + import results are cached, but keyed on the drivers-dir mtime so a
# `paiman install <voice pkg>` against a *running* web server is picked up without
# a restart. (The web server is a long-lived process independent of paiman — a
# kernel reload does not cycle it — so a process-lifetime cache would pin the
# driver set as it was when the first STT/TTS request landed.)
_lock = threading.Lock()
_packages_cache: list[dict] | None = None
_packages_cache_mtime: int | None = None
_module_cache: dict[str, object | None] = {}


def _drivers_dir():
    return PAI_ROOT / "usr" / "lib" / "drivers"


def _drivers_dir_mtime() -> int:
    """mtime (ns) of the drivers dir; changes when a driver is installed/removed."""
    try:
        return _drivers_dir().stat().st_mtime_ns
    except OSError:
        return -1


def _discover_packages() -> list[dict]:
    """Scan installed drivers for `provides`; return [{name, provides, voice_mode}]."""
    global _packages_cache, _packages_cache_mtime
    mtime = _drivers_dir_mtime()
    if _packages_cache is not None and _packages_cache_mtime == mtime:
        return _packages_cache
    # Driver set changed since the last scan: drop cached provider imports so a
    # removed driver's stale module can't be served. (Import failures aren't
    # cached — see _import_provider — so newly installed packages need no help
    # here; this only evicts successes of drivers that went away.)
    _module_cache.clear()
    found: list[dict] = []
    drivers_dir = _drivers_dir()
    if drivers_dir.is_dir():
        for pkg_yaml in sorted(drivers_dir.glob("*/package.yaml")):
            try:
                meta = yaml.safe_load(pkg_yaml.read_text(encoding="utf-8")) or {}
            except Exception:
                continue
            provides = meta.get("provides")
            if not isinstance(provides, list) or not provides:
                continue
            found.append(
                {
                    "name": meta.get("name") or pkg_yaml.parent.name,
                    "provides": [str(p) for p in provides],
                    "voice_mode": str(meta.get("voice_mode") or ""),
                }
            )
    _packages_cache = found
    _packages_cache_mtime = mtime
    return found


def _voice_config() -> dict:
    """Read the top-level `voice:` section of /etc/config.yaml (tolerant)."""
    try:
        data = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    voice = data.get("voice") if isinstance(data, dict) else None
    return voice if isinstance(voice, dict) else {}


def _import_provider(name: str):
    """Import `drivers.<name>.provider`, caching successes only. None on miss.

    Import *failures* are deliberately not cached: a package whose native deps
    aren't yet provisioned (ImportError) should be retried on the next request,
    so it goes live the moment `paiman install` finishes wiring the venv — even
    when that didn't change the drivers-dir mtime (e.g. a re-run of install.sh
    that only touches the venv). importlib caches the successful import in
    sys.modules, so the retry cost on the failure path is just a fast re-raise.
    """
    if name in _module_cache:
        return _module_cache[name]
    try:
        mod = importlib.import_module(f"drivers.{name}.provider")
    except Exception:
        # ImportError (deps not provisioned) or anything else → treat as absent,
        # but do NOT cache — retry next time in case deps get provisioned.
        return None
    _module_cache[name] = mod
    return mod


def _mode_rank(voice_mode: str) -> int:
    # Lower sorts first: prefer local, then cloud, then unspecified.
    return {"local": 0, "cloud": 1}.get(voice_mode, 2)


def _candidates(capability: str) -> list[str]:
    """Ordered package names to try for `capability`: configured pin first, then
    installed providers (local before cloud)."""
    cfg = _voice_config()
    pinned = cfg.get(capability) or cfg.get("provider")
    ordered: list[str] = []
    if isinstance(pinned, str) and pinned:
        ordered.append(pinned)
    for pkg in sorted(_discover_packages(), key=lambda p: _mode_rank(p["voice_mode"])):
        if capability in pkg["provides"] and pkg["name"] not in ordered:
            ordered.append(pkg["name"])
    return ordered


def resolve_provider(capability: str):
    """Return an imported provider module exposing `capability`, or None.

    `capability` is "stt" or "tts". None means no installed/importable package
    offers it — callers decide the fallback (macOS `say` for tts; an error for
    stt).
    """
    if capability not in _CAPABILITIES:
        raise ValueError(f"unknown voice capability: {capability!r}")
    func = "transcribe" if capability == "stt" else "synthesize"
    with _lock:
        for name in _candidates(capability):
            mod = _import_provider(name)
            if mod is not None and callable(getattr(mod, func, None)):
                return mod
    return None


def active_provider_name(capability: str) -> str | None:
    """Name of the package that would serve `capability`, for diagnostics."""
    mod = resolve_provider(capability)
    if mod is None:
        return None
    # drivers.<name>.provider → <name>
    return mod.__name__.split(".")[1] if "." in mod.__name__ else mod.__name__


def reset_cache() -> None:
    """Drop discovery/import caches (tests, or after a live `paiman` install)."""
    global _packages_cache, _packages_cache_mtime
    with _lock:
        _packages_cache = None
        _packages_cache_mtime = None
        _module_cache.clear()
