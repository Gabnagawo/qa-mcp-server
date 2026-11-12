"""A minimal tool-calling agent loop that drives the MCP server.

This is the reference *client*: it lists the server's tools, hands them to an
LLM (Ollama by default, Anthropic or OpenAI optionally) and executes the tool
calls the model asks for, until it answers without tools or runs out of steps.

Provider selection uses ``LLM_PROVIDER=ollama|anthropic|openai`` and
``LLM_MODEL``. The Anthropic and OpenAI SDKs are optional extras and are only
imported when selected.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

import anyio.to_thread
import httpx
from mcp import ClientSession
from mcp.types import CallToolResult, ImageContent, TextContent

DEFAULT_MODELS = {"ollama": "qwen3:8b", "anthropic": "claude-sonnet-5-5"}
MAX_TOOL_RESULT_CHARS = 6000

SYSTEM_PROMPT = """\
You are an exploratory tester driving a real Android device through tools.
Rules:
- Before interacting, call dump_ui to see the current screen. Never guess element names.
- Prefer tap with resource_id, text or content_desc copied from dump_ui over raw x/y.
- To type, tap the input field first, then call input_text.
- After every risky action (save, export, delete, submit) call logcat with level "E"
  to look for crashes (FATAL EXCEPTION) and call dump_ui to see what happened.
- Keep going until the goal is covered, then stop calling tools and answer with a short
  test report: steps performed, observed result, and any bug with its evidence
  (logcat lines). Do not invent results you did not observe."""


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class Message:
    role: Literal["user", "assistant", "tool"]
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None


@dataclass(frozen=True)
class AssistantTurn:
    text: str
    tool_calls: list[ToolCall]


class ChatProvider(Protocol):
    name: str
    model: str

    def chat(
        self, system: str, messages: list[Message], tools: list[ToolSpec]
    ) -> AssistantTurn: ...


_TEXT_TOOL_CALL_RE = re.compile(
    r"(?:<tool_call>)?\s*(\{\s*\"name\".*?\})\s*</tool_call>", flags=re.DOTALL
)


def extract_text_tool_calls(text: str) -> tuple[str, list[ToolCall]]:
    """Recover Hermes-style ``<tool_call>{...}</tool_call>`` blocks a model wrote as text.

    Small local models sometimes print the tool call instead of emitting it through
    the API, which would otherwise end the session as if it were the final answer.
    """
    calls = []
    for match in _TEXT_TOOL_CALL_RE.finditer(text):
        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and isinstance(data.get("name"), str):
            args = data.get("arguments") or {}
            calls.append(ToolCall(id=_new_id(), name=data["name"], arguments=dict(args)))
    if calls:
        text = _TEXT_TOOL_CALL_RE.sub("", text).strip()
    return text, calls


def _new_id() -> str:
    return "call_" + uuid.uuid4().hex[:12]


class OllamaProvider:
    """Ollama ``/api/chat`` with native tool calling. Zero cost, runs locally."""

    name = "ollama"

    def __init__(
        self,
        model: str = DEFAULT_MODELS["ollama"],
        base_url: str = "http://localhost:11434",
        client: httpx.Client | None = None,
        timeout_s: float = 300.0,
        num_ctx: int = 16384,
        retries: int = 2,
        think: bool = False,
    ) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.client = client or httpx.Client(timeout=timeout_s)
        # Ollama defaults to a small context window; an exploratory session with UI dumps
        # outgrows it within a few steps.
        self.num_ctx = num_ctx
        self.retries = retries
        self.think = think

    def chat(self, system: str, messages: list[Message], tools: list[ToolSpec]) -> AssistantTurn:
        wire: list[dict[str, Any]] = [{"role": "system", "content": system}]
        for msg in messages:
            if msg.role == "tool":
                wire.append({"role": "tool", "content": msg.content, "tool_name": msg.name})
            elif msg.role == "assistant":
                entry: dict[str, Any] = {"role": "assistant", "content": msg.content}
                if msg.tool_calls:
                    entry["tool_calls"] = [
                        {"function": {"name": c.name, "arguments": c.arguments}}
                        for c in msg.tool_calls
                    ]
                wire.append(entry)
            else:
                wire.append({"role": "user", "content": msg.content})
        payload = {
            "model": self.model,
            "messages": wire,
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    },
                }
                for t in tools
            ],
            "stream": False,
            "think": self.think,
            # Qwen3 loops under greedy decoding; use its recommended non-thinking sampling,
            # seeded, with a cap on generated tokens.
            "options": {
                "temperature": 0.7,
                "top_p": 0.8,
                "top_k": 20,
                "seed": 7,
                "num_ctx": self.num_ctx,
                "num_predict": 1024,
            },
        }
        for attempt in range(self.retries + 1):
            # A new seed per attempt: with the same seed a degenerate sample (Ollama's
            # "token repeat limit reached") would simply repeat.
            payload["options"]["seed"] = 7 + attempt
            response = self.client.post(f"{self.base_url}/api/chat", json=payload)
            if response.status_code < 500 or attempt == self.retries:
                break
        if response.is_error:
            raise httpx.HTTPStatusError(
                f"Ollama returned {response.status_code}: {response.text[:300]}",
                request=response.request,
                response=response,
            )
        message = response.json().get("message", {})
        calls = []
        for raw in message.get("tool_calls") or []:
            fn = raw.get("function", {})
            args = fn.get("arguments") or {}
            if isinstance(args, str):
                args = json.loads(args)
            calls.append(ToolCall(id=_new_id(), name=fn.get("name", ""), arguments=args))
        text = message.get("content", "") or ""
        if not calls:
            text, calls = extract_text_tool_calls(text)
        return AssistantTurn(text=text, tool_calls=calls)


class OpenAIProvider:
    """OpenAI chat completions with function tools (``pip install qa-mcp-server[openai]``)."""

    name = "openai"

    def __init__(self, model: str, client: Any | None = None) -> None:
        if client is None:
            import openai  # lazy optional import

            client = openai.OpenAI()
        self.model = model
        self.client = client

    def chat(self, system: str, messages: list[Message], tools: list[ToolSpec]) -> AssistantTurn:
        wire: list[dict[str, Any]] = [{"role": "system", "content": system}]
        for msg in messages:
            if msg.role == "tool":
                wire.append(
                    {"role": "tool", "tool_call_id": msg.tool_call_id, "content": msg.content}
                )
            elif msg.role == "assistant":
                entry: dict[str, Any] = {"role": "assistant", "content": msg.content or None}
                if msg.tool_calls:
                    entry["tool_calls"] = [
                        {
                            "id": c.id,
                            "type": "function",
                            "function": {"name": c.name, "arguments": json.dumps(c.arguments)},
                        }
                        for c in msg.tool_calls
                    ]
                wire.append(entry)
            else:
                wire.append({"role": "user", "content": msg.content})
        response = self.client.chat.completions.create(
            model=self.model,
            messages=wire,
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    },
                }
                for t in tools
            ],
        )
        message = response.choices[0].message
        calls = [
            ToolCall(
                id=c.id, name=c.function.name, arguments=json.loads(c.function.arguments or "{}")
            )
            for c in message.tool_calls or []
        ]
        return AssistantTurn(text=message.content or "", tool_calls=calls)


class AnthropicProvider:
    """Anthropic Messages API with tool use (``pip install qa-mcp-server[anthropic]``)."""

    name = "anthropic"

    def __init__(self, model: str = DEFAULT_MODELS["anthropic"], client: Any | None = None) -> None:
        if client is None:
            import anthropic  # lazy optional import

            client = anthropic.Anthropic()
        self.model = model
        self.client = client

    def chat(self, system: str, messages: list[Message], tools: list[ToolSpec]) -> AssistantTurn:
        wire: list[dict[str, Any]] = []
        for msg in messages:
            if msg.role == "tool":
                block = {
                    "type": "tool_result",
                    "tool_use_id": msg.tool_call_id,
                    "content": msg.content,
                }
                # Consecutive tool results must share one user turn.
                if wire and wire[-1]["role"] == "user" and isinstance(wire[-1]["content"], list):
                    wire[-1]["content"].append(block)
                else:
                    wire.append({"role": "user", "content": [block]})
            elif msg.role == "assistant":
                content: list[dict[str, Any]] = []
                if msg.content:
                    content.append({"type": "text", "text": msg.content})
                content.extend(
                    {"type": "tool_use", "id": c.id, "name": c.name, "input": c.arguments}
                    for c in msg.tool_calls
                )
                wire.append({"role": "assistant", "content": content})
            else:
                wire.append({"role": "user", "content": msg.content})
        response = self.client.messages.create(
            model=self.model,
            max_tokens=2048,
            system=system,
            messages=wire,
            tools=[
                {"name": t.name, "description": t.description, "input_schema": t.parameters}
                for t in tools
            ],
        )
        text = "".join(b.text for b in response.content if b.type == "text")
        calls = [
            ToolCall(id=b.id, name=b.name, arguments=dict(b.input))
            for b in response.content
            if b.type == "tool_use"
        ]
        return AssistantTurn(text=text, tool_calls=calls)


def provider_from_env(env: Mapping[str, str] | None = None) -> ChatProvider:
    env = os.environ if env is None else env
    kind = env.get("LLM_PROVIDER", "ollama").strip().lower()
    model = env.get("LLM_MODEL", "").strip()
    if kind == "ollama":
        return OllamaProvider(
            model=model or DEFAULT_MODELS["ollama"],
            base_url=env.get("OLLAMA_HOST", "http://localhost:11434"),
            num_ctx=int(env.get("OLLAMA_NUM_CTX", "16384")),
            think=env.get("OLLAMA_THINK", "false").strip().lower() in {"1", "true", "yes"},
        )
    if kind == "anthropic":
        return AnthropicProvider(model=model or DEFAULT_MODELS["anthropic"])
    if kind == "openai":
        if not model:
            raise ValueError("LLM_PROVIDER=openai needs LLM_MODEL")
        return OpenAIProvider(model=model)
    raise ValueError(f"unknown LLM_PROVIDER {kind!r} (expected ollama, anthropic or openai)")


def render_tool_result(result: CallToolResult) -> str:
    """Flatten a tool result to text for the model; images are summarised, not inlined."""
    parts = []
    if result.structuredContent is not None and not result.isError:
        # Compact JSON: the pretty-printed text block costs roughly twice the tokens.
        parts.append(json.dumps(result.structuredContent, separators=(",", ":")))
    for block in result.content:
        if parts and result.structuredContent is not None:
            break
        if isinstance(block, TextContent):
            parts.append(block.text)
        elif isinstance(block, ImageContent):
            parts.append(f"[{block.mimeType} image, {len(block.data) * 3 // 4} bytes, not shown]")
    text = "\n".join(parts)
    if result.isError:
        text = "ERROR: " + text
    if len(text) > MAX_TOOL_RESULT_CHARS:
        text = text[:MAX_TOOL_RESULT_CHARS] + f"... [truncated, {len(text)} chars]"
    return text


@dataclass
class StepRecord:
    tool: str
    arguments: dict[str, Any]
    is_error: bool
    result_preview: str


@dataclass
class ExplorationReport:
    provider: str
    model: str
    steps: list[StepRecord]
    final_answer: str
    stopped_reason: Literal["answered", "max_steps", "provider_error"]


async def explore(
    session: ClientSession,
    provider: ChatProvider,
    goal: str,
    *,
    max_steps: int = 20,
    on_step: Callable[[StepRecord], None] | None = None,
) -> ExplorationReport:
    """Run the agent loop until the model answers without tool calls or ``max_steps``."""
    listed = await session.list_tools()
    specs = [
        ToolSpec(name=t.name, description=t.description or "", parameters=t.inputSchema)
        for t in listed.tools
    ]
    messages = [Message(role="user", content=goal)]
    steps: list[StepRecord] = []
    for _ in range(max_steps):
        try:
            turn = await anyio.to_thread.run_sync(provider.chat, SYSTEM_PROMPT, messages, specs)
        except Exception as exc:  # network, HTTP or SDK errors end the session, not the process
            return ExplorationReport(
                provider.name, provider.model, steps, f"provider error: {exc}", "provider_error"
            )
        messages.append(Message(role="assistant", content=turn.text, tool_calls=turn.tool_calls))
        if not turn.tool_calls:
            return ExplorationReport(provider.name, provider.model, steps, turn.text, "answered")
        for call in turn.tool_calls:
            result = await session.call_tool(call.name, call.arguments)
            text = render_tool_result(result)
            record = StepRecord(call.name, call.arguments, bool(result.isError), text[:400])
            steps.append(record)
            if on_step is not None:
                on_step(record)
            messages.append(
                Message(role="tool", content=text, tool_call_id=call.id, name=call.name)
            )
    return ExplorationReport(provider.name, provider.model, steps, "", "max_steps")
