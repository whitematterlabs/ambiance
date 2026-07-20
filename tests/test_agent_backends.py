"""Backend modularity: each wire owns its shapes; the registry routes;
the turn engine reseeds across wire families instead of translating."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from agent import llm
from agent.backends import anthropic as awire
from agent.backends import base, for_wire
from agent.backends import openai as owire
from agent.turn import Engine


# --- registry ---------------------------------------------------------------

def test_wire_routing():
    assert for_wire("anthropic") is awire
    assert for_wire("openai") is owire
    assert llm.wire_for("anthropic") == "anthropic"
    assert llm.wire_for("deepseek") == "anthropic"
    assert llm.wire_for("openai") == "openai"
    assert llm.wire_for("openrouter") == "openai"
    with pytest.raises(ValueError):
        llm.wire_for("litellm")
    with pytest.raises(ValueError):
        for_wire("carrier-pigeon")


# --- anthropic wire ---------------------------------------------------------

class _Block:
    def __init__(self, **kw):
        self.__dict__.update(kw)

    def model_dump(self):
        return dict(self.__dict__)


class _FakeAnthropic:
    """Scripted client: first call requests a bash echo, second replies."""

    def __init__(self):
        self.requests = []
        self.messages = SimpleNamespace(create=self._create)

    async def _create(self, **kw):
        self.requests.append(kw)
        usage = SimpleNamespace(
            input_tokens=10, output_tokens=5,
            cache_read_input_tokens=0, cache_creation_input_tokens=0,
        )
        if len(self.requests) == 1:
            return SimpleNamespace(
                content=[
                    _Block(type="text", text="running echo"),
                    _Block(type="tool_use", id="tu_1", name="bash",
                           input={"command": "echo backend-test"}),
                ],
                usage=usage,
            )
        return SimpleNamespace(
            content=[_Block(type="text", text="done")], usage=usage
        )


def test_anthropic_wire_tool_round_trip(tmp_path):
    client = _FakeAnthropic()
    key, spec, model = llm.resolve("anthropic", None)
    reply, messages = asyncio.run(
        awire.run_turn(
            key, spec, model, "system", "please echo",
            state_dir=tmp_path, home=tmp_path, client=client,
        )
    )
    assert reply == "done"
    # Anthropic shapes: assistant turns are block lists; the tool result
    # rides a user turn keyed by tool_use_id.
    assistant = messages[1]
    assert assistant["role"] == "assistant"
    assert any(b.get("type") == "tool_use" for b in assistant["content"])
    result_turn = messages[2]
    assert result_turn["role"] == "user"
    block = result_turn["content"][0]
    assert block["type"] == "tool_result" and block["tool_use_id"] == "tu_1"
    assert "backend-test" in str(block["content"])
    # Prompt-cache idiom: tail block of the request is cache-marked.
    tail = client.requests[1]["messages"][-1]["content"][-1]
    assert tail.get("cache_control") == {"type": "ephemeral"}


# --- openai wire ------------------------------------------------------------

class _FakeOpenAI:
    def __init__(self):
        self.requests = []
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self._create)
        )

    async def _create(self, **kw):
        self.requests.append(kw)
        usage = SimpleNamespace(
            prompt_tokens=10, completion_tokens=5, prompt_tokens_details=None
        )
        if len(self.requests) == 1:
            call = SimpleNamespace(
                id="call_1",
                function=SimpleNamespace(
                    name="bash",
                    arguments=json.dumps({"command": "echo backend-test"}),
                ),
            )
            msg = SimpleNamespace(content=None, tool_calls=[call])
        else:
            msg = SimpleNamespace(content="done", tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=usage)


def test_openai_wire_tool_round_trip(tmp_path):
    client = _FakeOpenAI()
    key, spec, model = llm.resolve("openai", None)
    reply, messages = asyncio.run(
        owire.run_turn(
            key, spec, model, "system", "please echo",
            state_dir=tmp_path, home=tmp_path, client=client,
        )
    )
    assert reply == "done"
    # OpenAI shapes: tool_calls on the assistant message (arguments as JSON
    # strings), answered by role:"tool" keyed by tool_call_id.
    assistant = messages[1]
    assert assistant["role"] == "assistant"
    assert assistant["tool_calls"][0]["function"]["name"] == "bash"
    tool_msg = messages[2]
    assert tool_msg["role"] == "tool" and tool_msg["tool_call_id"] == "call_1"
    assert "backend-test" in tool_msg["content"]
    # System prompt is prepended per call, never persisted in history.
    assert client.requests[0]["messages"][0]["role"] == "system"
    assert all(m.get("role") != "system" for m in messages)
    # Both wires render the same JSON Schema, wrapped per wire.
    fn = client.requests[0]["tools"][0]["function"]
    assert fn["parameters"] == base.TOOL_SCHEMAS[0]["input_schema"]


def test_openai_wire_malformed_arguments(tmp_path):
    class _Bad(_FakeOpenAI):
        async def _create(self, **kw):
            self.requests.append(kw)
            usage = SimpleNamespace(
                prompt_tokens=1, completion_tokens=1, prompt_tokens_details=None
            )
            if len(self.requests) == 1:
                call = SimpleNamespace(
                    id="call_1",
                    function=SimpleNamespace(name="bash", arguments="{not json"),
                )
                msg = SimpleNamespace(content=None, tool_calls=[call])
            else:
                msg = SimpleNamespace(content="ok", tool_calls=None)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=msg)], usage=usage
            )

    client = _Bad()
    key, spec, model = llm.resolve("openai", None)
    reply, messages = asyncio.run(
        owire.run_turn(
            key, spec, model, "s", "u",
            state_dir=tmp_path, home=tmp_path, client=client,
        )
    )
    assert reply == "ok"
    assert "ERROR" in messages[2]["content"]


# --- wire switch ------------------------------------------------------------

def test_wire_switch_archives_and_reseeds(tmp_path):
    home = tmp_path / "home"
    state = tmp_path / "state"
    engine = Engine("tester", {"provider": "anthropic"}, home=home, state_dir=state)
    engine.save_history([
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ])
    engine.ensure_wire()  # stamps anthropic; no reseed
    assert engine.load_history()[0]["content"] == "hi"

    switched = Engine("tester", {"provider": "openai"}, home=home, state_dir=state)
    switched.ensure_wire()
    history = switched.load_history()
    assert "wire changed anthropic → openai" in history[0]["content"]
    archives = list((state / "session" / "history").glob("*-wireswitch.jsonl"))
    assert len(archives) == 1
    # Idempotent once stamped.
    switched.ensure_wire()
    assert len(switched.load_history()) == 2
