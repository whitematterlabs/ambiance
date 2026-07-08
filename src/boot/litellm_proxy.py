"""LiteLLM proxy — kernel-supervised translator for non-Anthropic providers.

PAI's hot tool loop only speaks the Anthropic Messages wire protocol
(boot/llm.py). Providers that aren't Anthropic-wire-compatible — OpenAI's
native API, notably — can't be reached directly by `AsyncAnthropic`. LiteLLM
exposes a native Anthropic `/v1/messages` endpoint that translates Anthropic
requests (including the full tool_use/tool_result loop) to OpenAI and back, so
an `openai` PAI just points its client at `127.0.0.1:PROXY_PORT` exactly the
way DeepSeek points at its `/anthropic` endpoint. The hot path gets zero new
branching.

This is infra, not a driver: it owns no on-disk surface of its own (no
/sys/drivers entry, no events.yaml). It reuses boot/supervisor.py for the
subprocess fork / log-tee / restart machinery and is tracked at
/proc/litellm-proxy/ like any supervised service. Teardown on kernel exit is
already handled by supervisor.shutdown() in main.run()'s finally block.

Tickless: the proxy is spawned only when the fleet actually contains a
provider whose ProviderSpec.via_proxy is True. An all-Anthropic/DeepSeek fleet
never starts it.
"""

from __future__ import annotations

import asyncio
import socket
import sys
from pathlib import Path

import yaml

from . import config as C
from . import llm as L
from . import paths
from . import processes as P
from . import supervisor

PROXY_SLUG = "litellm-proxy"

# How long to wait for the freshly-forked proxy to accept connections before
# giving up and returning anyway (the first proxied turn would then retry).
_READINESS_TIMEOUT = 30.0
_READINESS_POLL = 0.25

# Upstream guards baked into the generated proxy config. request_timeout bounds
# a single proxy→upstream call; num_retries lets the proxy abandon+retry a
# stalled upstream itself. Kept below boot/llm._CLIENT_TIMEOUT.read so LiteLLM
# returns a clean error before the kernel's raw client timeout fires; this is
# the backstop that turns a wedged upstream into a ~5-min failure, not ~10.
_REQUEST_TIMEOUT = 120
_NUM_RETRIES = 1


def _provider_is_proxied(provider: str | None) -> bool:
    """True iff the effective provider routes through the proxy. Unknown
    providers (validation rejects them upstream) are treated as not-proxied."""
    key = provider or L.DEFAULT_PROVIDER
    spec = L.PROVIDERS.get(key)
    return bool(spec and spec.via_proxy)


def _proxied_providers(config: dict[str, dict] | None = None) -> set[str]:
    """Set of provider keys in the fleet that route through the proxy."""
    if config is None:
        config = C.load_config()
    found: set[str] = set()
    for spec in config.values():
        provider = spec.get("provider")
        if _provider_is_proxied(provider):
            found.add(provider or L.DEFAULT_PROVIDER)
    return found


def fleet_needs_proxy(config: dict[str, dict] | None = None) -> bool:
    """Does any fleet member use a proxied provider?"""
    return bool(_proxied_providers(config))


def _proxy_argv(cfg: Path) -> list[str]:
    """Build argv for the proxy server.

    LiteLLM ships no `__main__`, so `python -m litellm` does not work. The
    supported entrypoint is the `litellm` console script (`litellm:run_server`),
    which pip installs alongside the venv python. We look for it next to
    `sys.executable` (NOT its realpath — the venv's python is a symlink to the
    base interpreter, whose dir does not hold the venv's console scripts) so the
    proxy runs under the same venv as the kernel. If the script is missing
    (unusual layout), fall back to running the proxy CLI module directly.
    """
    script = Path(sys.executable).parent / "litellm"
    if script.exists():
        head: list[str] = [str(script)]
    else:
        head = [sys.executable, "-m", "litellm.proxy.proxy_cli"]
    return head + [
        "--config",
        str(cfg),
        "--host",
        "127.0.0.1",
        "--port",
        str(L.PROXY_PORT),
    ]


def _config_path() -> Path:
    return paths.run() / "litellm" / "config.yaml"


def _write_config() -> Path:
    """Generate LiteLLM's own config under run/ (ephemeral, not committed).

    One model_list row per proxied provider in the fleet, namespaced by the
    provider's proxy_prefix so a request the kernel sends as "<prefix>/<model>"
    (see boot/llm._resolve) routes to that provider's upstream — not blanket
    OpenAI. Per-provider wildcard keeps zero per-model maintenance while
    differentiating endpoints.
    """
    model_list = []
    for key in sorted(_proxied_providers()):
        spec = L.PROVIDERS[key]
        prefix = spec.proxy_prefix or key
        params: dict = {
            "model": f"{prefix}/*",
            "api_key": f"os.environ/{spec.api_key_env}",
        }
        if spec.proxy_api_base:
            params["api_base"] = spec.proxy_api_base
        model_list.append({"model_name": f"{prefix}/*", "litellm_params": params})
    cfg = {
        "model_list": model_list,
        "litellm_settings": {
            # See original note: bridge /v1/messages through chat/completions so
            # the cost logger validates cleanly for gpt-5.x.
            "use_chat_completions_url_for_anthropic_messages": True,
            "request_timeout": _REQUEST_TIMEOUT,
            "num_retries": _NUM_RETRIES,
        },
    }
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        yaml.safe_dump(cfg, f, sort_keys=False)
    return path


async def _await_ready() -> bool:
    """Poll a TCP connect to the proxy port until it accepts or we time out.

    Returns True once the port is listening so the first proxied turn doesn't
    race a cold start; False on timeout (the proxy may still come up — the
    first turn just retries against it)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _READINESS_TIMEOUT
    while loop.time() < deadline:
        try:
            fut = asyncio.open_connection("127.0.0.1", L.PROXY_PORT)
            reader, writer = await asyncio.wait_for(fut, timeout=_READINESS_POLL)
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            return True
        except (ConnectionRefusedError, OSError, asyncio.TimeoutError):
            await asyncio.sleep(_READINESS_POLL)
    return False


def _ensure_proc() -> None:
    """Idempotent /proc/litellm-proxy/ entry. Mirrors main._ensure_driver_proc:
    first spawn writes the spec; subsequent calls just reset status to running
    so a prior cancelled/failed resolution doesn't make /proc look terminal."""
    proc = P.PROC_DIR / PROXY_SLUG
    if proc.exists():
        (proc / "status").write_text("running\n")
        try:
            P.append_log(PROXY_SLUG, "kernel: restarted")
        except P.ProcessNotFound:
            pass
    else:
        P.spawn(PROXY_SLUG, {"kind": "infra"})


async def _spawn() -> None:
    """Write a fresh config and start the proxy under the supervisor."""
    cfg = _write_config()
    _ensure_proc()
    spec = {
        "run": _proxy_argv(cfg),
        "restart": "always",
    }
    await supervisor.start(PROXY_SLUG, spec)
    ready = await _await_ready()
    if ready:
        P.append_log(PROXY_SLUG, f"kernel: proxy ready on 127.0.0.1:{L.PROXY_PORT}")
    else:
        P.append_log(
            PROXY_SLUG,
            f"kernel: proxy not ready after {_READINESS_TIMEOUT}s "
            "(first turn will retry)",
        )


async def _stop() -> None:
    """Stop the supervised proxy without tripping its restart policy."""
    # Resolve to a non-running status BEFORE stopping so the supervisor's
    # _await_exit sees a terminal status and does not restart-loop a proxy
    # we deliberately killed (restart policy is "always").
    try:
        P.resolve(PROXY_SLUG, "stopped")
    except P.ProcessNotFound:
        pass
    await supervisor.stop(PROXY_SLUG)


async def reconcile(event: dict | None = None) -> None:
    """Bring the proxy into sync with the fleet's need for it. Idempotent.

    Called at boot (after driver reconcile) and on every kernel:reload_config,
    so adding/removing an `openai` PAI at runtime starts/stops the proxy with
    no reboot. Never on a timer.

    A running proxy can also go stale in place: its config is written once at
    spawn (one model_list row per proxied provider), and it resolves
    `os.environ/<API_KEY_ENV>` from its own process env, snapshotted at fork.
    So when the proxy is needed AND already running, regenerate the config and
    restart on any content change (e.g. a PAI switched openai→openrouter), and
    also restart when the triggering reload is a `set-api-key` for a proxied
    provider even if the config bytes are unchanged — the key lives in the
    proxy's env, not the config. `event` is the kernel:reload_config payload
    when the caller has one.
    """
    needs = fleet_needs_proxy()
    tracked = supervisor.is_tracked(PROXY_SLUG)

    if needs and not tracked:
        await _spawn()
    elif not needs and tracked:
        await _stop()
    elif needs and tracked:
        # The on-disk config always matches the running proxy (spawn writes it,
        # and any change below triggers a restart), so read-before/compare-after
        # detects drift without extra bookkeeping.
        path = _config_path()
        try:
            before = path.read_bytes()
        except OSError:
            before = None
        _write_config()
        changed = path.read_bytes() != before
        key_changed = bool(
            event
            and event.get("action") == "set-api-key"
            and event.get("provider")
            and _provider_is_proxied(event.get("provider"))
        )
        if changed or key_changed:
            reason = (
                "config changed" if changed
                else f"{event.get('provider')} key updated"  # type: ignore[union-attr]
            )
            try:
                P.append_log(PROXY_SLUG, f"kernel: restarting proxy ({reason})")
            except P.ProcessNotFound:
                pass
            await _stop()
            await _spawn()
