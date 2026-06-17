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


def _provider_is_proxied(provider: str | None) -> bool:
    """True iff the effective provider routes through the proxy. Unknown
    providers (validation rejects them upstream) are treated as not-proxied."""
    key = provider or L.DEFAULT_PROVIDER
    spec = L.PROVIDERS.get(key)
    return bool(spec and spec.via_proxy)


def fleet_needs_proxy(config: dict[str, dict] | None = None) -> bool:
    """Does any fleet member — or any dependency persub — use a proxied provider?

    Mirrors config._reconcile_persubs' provider-resolution chain for deps
    (dep override → bundle → parent) so a dependency that only inherits its
    provider from a packaged bundle is still counted.
    """
    if config is None:
        config = C.load_config()
    for parent_spec in config.values():
        if _provider_is_proxied(parent_spec.get("provider")):
            return True
        for dep in parent_spec.get("dependencies") or []:
            bundle: dict = {}
            pkg = dep.get("package")
            if pkg:
                try:
                    bundle = C.resolve_subagent_package(pkg)
                except Exception:
                    bundle = {}
            provider = (
                dep.get("provider")
                or bundle.get("provider")
                or parent_spec.get("provider")
            )
            if _provider_is_proxied(provider):
                return True
    return False


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
    """Generate LiteLLM's own config under run/ (ephemeral, not committed —
    honors the no-arbitrary-scaffolding rule). Wildcard routing means any
    OpenAI model name works with zero per-model maintenance."""
    cfg = {
        "model_list": [
            {
                "model_name": "*",
                "litellm_params": {"model": "openai/*"},
            }
        ],
        # Route OpenAI's /v1/messages bridge through chat/completions, not the
        # Responses API. LiteLLM's _should_route_to_responses_api() sends
        # provider "openai" down the Responses path, but its success/cost
        # logger then does AnthropicResponse.model_validate(result) on a
        # ResponsesAPIResponse and throws a (non-blocking, but log-spamming)
        # validation error on every call — and breaks cost tracking for gpt-5.x.
        # The chat/completions bridge yields an Anthropic-shaped result the
        # logger validates cleanly. This flag is the upstream-documented opt-out
        # (litellm.use_chat_completions_url_for_anthropic_messages).
        "litellm_settings": {
            "use_chat_completions_url_for_anthropic_messages": True,
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


async def reconcile() -> None:
    """Bring the proxy into sync with the fleet's need for it. Idempotent.

    Called at boot (after driver reconcile) and on every kernel:reload_config,
    so adding/removing an `openai` PAI at runtime starts/stops the proxy with
    no reboot. Never on a timer.
    """
    needs = fleet_needs_proxy()
    tracked = supervisor.is_tracked(PROXY_SLUG)

    if needs and not tracked:
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
    elif not needs and tracked:
        # Resolve to a non-running status BEFORE stopping so the supervisor's
        # _await_exit sees a terminal status and does not restart-loop a proxy
        # we deliberately killed (restart policy is "always").
        try:
            P.resolve(PROXY_SLUG, "stopped")
        except P.ProcessNotFound:
            pass
        await supervisor.stop(PROXY_SLUG)
