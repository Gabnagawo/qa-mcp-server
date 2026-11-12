"""Agent loop and provider adapters, without any live LLM."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from mcp import ClientSession
from mcp.types import CallToolResult, ImageContent, TextContent

from qa_mcp_server.agent import (
    AnthropicProvider,
    AssistantTurn,
    Message,
    OllamaProvider,
    OpenAIProvider,
    ToolCall,
    ToolSpec,
    explore,
    provider_from_env,
    render_tool_result,
)

from .conftest import APP

TOOLS = [ToolSpec(name="dump_ui", description="d", parameters={"type": "object"})]
HISTORY = [
    Message(role="user", content="go"),
    Message(
        role="assistant",
        content="looking",
        tool_calls=[ToolCall(id="c1", name="dump_ui", arguments={"compact": True})],
    ),
    Message(role="tool", content="{}", tool_call_id="c1", name="dump_ui"),
    Message(role="tool", content="{}", tool_call_id="c2", name="dump_ui"),
]


class ScriptedProvider:
    name = "scripted"
    model = "fake-1"

    def __init__(self, turns: list[AssistantTurn]) -> None:
        self.turns = turns
        self.seen: list[list[Message]] = []

    def chat(self, system: str, messages: list[Message], tools: list[ToolSpec]) -> AssistantTurn:
        assert "exploratory tester" in system
        assert {t.name for t in tools} >= {"dump_ui", "tap"}
        self.seen.append(list(messages))
        return self.turns.pop(0)


def call(name: str, **arguments: Any) -> AssistantTurn:
    return AssistantTurn(text="", tool_calls=[ToolCall(id=name, name=name, arguments=arguments)])


@pytest.mark.anyio
async def test_explore_runs_tool_calls_until_answer(client: ClientSession) -> None:
    provider = ScriptedProvider(
        [
            call("install_apk", apk_path="sample-notes-1.0.apk"),
            call("launch_app", package=APP),
            call("input_text", text="bad; text"),
            AssistantTurn(text="Report: no bugs found.", tool_calls=[]),
        ]
    )
    seen_steps: list[str] = []
    report = await explore(
        client, provider, "test it", max_steps=10, on_step=lambda s: seen_steps.append(s.tool)
    )
    assert report.stopped_reason == "answered"
    assert report.final_answer == "Report: no bugs found."
    assert seen_steps == ["install_apk", "launch_app", "input_text"]
    assert report.steps[2].is_error
    last_history = provider.seen[-1]
    assert last_history[-1].role == "tool"
    assert last_history[-1].content.startswith("ERROR:")


@pytest.mark.anyio
async def test_explore_stops_at_max_steps(client: ClientSession) -> None:
    provider = ScriptedProvider([call("list_devices"), call("list_devices")])
    report = await explore(client, provider, "loop", max_steps=2)
    assert report.stopped_reason == "max_steps"
    assert len(report.steps) == 2


def test_render_tool_result_summarises_images_and_truncates() -> None:
    result = CallToolResult(
        content=[
            TextContent(type="text", text="x" * 7000),
            ImageContent(type="image", data="AAAA", mimeType="image/png"),
        ],
        isError=False,
    )
    text = render_tool_result(result)
    assert text.endswith("chars]")
    assert len(text) < 6100
    short = render_tool_result(
        CallToolResult(content=[ImageContent(type="image", data="AAAA", mimeType="image/png")])
    )
    assert short == "[image/png image, 3 bytes, not shown]"


def test_ollama_provider_wire_format() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "message": {
                    "content": "",
                    "tool_calls": [
                        {"function": {"name": "tap", "arguments": {"text": "Save"}}},
                        {"function": {"name": "dump_ui", "arguments": '{"compact": false}'}},
                    ],
                }
            },
        )

    provider = OllamaProvider(client=httpx.Client(transport=httpx.MockTransport(handler)))
    turn = provider.chat("sys", HISTORY, TOOLS)
    assert [c.name for c in turn.tool_calls] == ["tap", "dump_ui"]
    assert turn.tool_calls[1].arguments == {"compact": False}
    assert captured["model"] == "qwen3:8b"
    assert captured["think"] is False
    assert captured["options"]["num_ctx"] == 16384
    roles = [m["role"] for m in captured["messages"]]
    assert roles == ["system", "user", "assistant", "tool", "tool"]
    assert captured["messages"][2]["tool_calls"][0]["function"]["name"] == "dump_ui"
    assert captured["tools"][0]["function"]["name"] == "dump_ui"


def test_openai_provider_wire_format() -> None:
    captured: dict[str, Any] = {}

    def create(**kwargs: Any) -> Any:
        captured.update(kwargs)
        tool_call = SimpleNamespace(
            id="x1", function=SimpleNamespace(name="tap", arguments='{"text": "Save"}')
        )
        message = SimpleNamespace(content=None, tool_calls=[tool_call])
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    turn = OpenAIProvider(model="gpt-test", client=client).chat("sys", HISTORY, TOOLS)
    assert turn.tool_calls == [ToolCall(id="x1", name="tap", arguments={"text": "Save"})]
    assert turn.text == ""
    wire = captured["messages"]
    assert wire[2]["tool_calls"][0]["function"]["arguments"] == '{"compact": true}'
    assert wire[3] == {"role": "tool", "tool_call_id": "c1", "content": "{}"}


def test_anthropic_provider_wire_format() -> None:
    captured: dict[str, Any] = {}

    def create(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return SimpleNamespace(
            content=[
                SimpleNamespace(type="text", text="I will tap."),
                SimpleNamespace(type="tool_use", id="t1", name="tap", input={"text": "Save"}),
            ]
        )

    client = SimpleNamespace(messages=SimpleNamespace(create=create))
    provider = AnthropicProvider(client=client)
    turn = provider.chat("sys", HISTORY, TOOLS)
    assert provider.model == "claude-sonnet-5-5"
    assert turn.text == "I will tap."
    assert turn.tool_calls == [ToolCall(id="t1", name="tap", arguments={"text": "Save"})]
    assert captured["system"] == "sys"
    wire = captured["messages"]
    assert [m["role"] for m in wire] == ["user", "assistant", "user"]
    assert [b["tool_use_id"] for b in wire[2]["content"]] == ["c1", "c2"]
    assert captured["tools"][0]["input_schema"] == {"type": "object"}


def test_ollama_retries_once_on_server_error() -> None:
    statuses = iter([500, 200])

    def handler(request: httpx.Request) -> httpx.Response:
        status = next(statuses)
        return httpx.Response(status, json={"message": {"content": "ok"}} if status == 200 else {})

    provider = OllamaProvider(client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert provider.chat("sys", HISTORY, TOOLS).text == "ok"

    failing = OllamaProvider(
        client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503))),
        retries=0,
    )
    with pytest.raises(httpx.HTTPStatusError):
        failing.chat("sys", HISTORY, TOOLS)


def test_render_prefers_compact_structured_content() -> None:
    result = CallToolResult(
        content=[TextContent(type="text", text='{\n  "a": 1\n}')],
        structuredContent={"a": 1},
    )
    assert render_tool_result(result) == '{"a":1}'


def test_provider_from_env() -> None:
    ollama = provider_from_env(
        {"OLLAMA_HOST": "http://gpu-box:11434/", "LLM_MODEL": "llama3.2", "OLLAMA_NUM_CTX": "8192"}
    )
    assert ollama.num_ctx == 8192
    assert isinstance(ollama, OllamaProvider)
    assert ollama.base_url == "http://gpu-box:11434"
    assert ollama.model == "llama3.2"
    with pytest.raises(ValueError, match="LLM_MODEL"):
        provider_from_env({"LLM_PROVIDER": "openai"})
    with pytest.raises(ValueError, match="unknown"):
        provider_from_env({"LLM_PROVIDER": "nope"})


def test_optional_sdks_are_lazy(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    fake_anthropic = SimpleNamespace(Anthropic=lambda: "anthropic-client")
    fake_openai = SimpleNamespace(OpenAI=lambda: "openai-client")
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic)
    monkeypatch.setitem(sys.modules, "openai", fake_openai)
    anthropic = provider_from_env({"LLM_PROVIDER": "anthropic"})
    openai = provider_from_env({"LLM_PROVIDER": "openai", "LLM_MODEL": "gpt-x"})
    assert isinstance(anthropic, AnthropicProvider)
    assert anthropic.client == "anthropic-client"
    assert isinstance(openai, OpenAIProvider)
    assert openai.client == "openai-client"
