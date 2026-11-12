"""Scripted exploratory session against the fake device, over the real stdio transport.

Starts ``python -m qa_mcp_server serve --fake`` as a subprocess, connects an MCP
client to it exactly like Claude Desktop would, walks through an exploratory
test of the synthetic "Sample Notes" app (which has a planted crash in its
"Export as PDF" action), throws a few injection attempts at it, and prints a
transcript. No device and no LLM needed.

    uv run python examples/demo_transcript.py
"""

from __future__ import annotations

import json
import shutil
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import get_default_environment, stdio_client
from mcp.types import CallToolResult, ImageContent, TextContent

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / "qa-mcp-artifacts" / "demo"
APP = "org.qamcp.sample.notes"


def summarize(tool: str, result: CallToolResult) -> str:
    if result.isError:
        text = next((b.text for b in result.content if isinstance(b, TextContent)), "")
        text = " ".join(text.removeprefix(f"Error executing tool {tool}: ").split())
        text = text.replace("For further information visit", "|").split(" |")[0]
        return "REJECTED: " + text[:170]
    images = [b for b in result.content if isinstance(b, ImageContent)]
    if images:
        return f"image/png, {len(images[0].data) * 3 // 4} bytes"
    data: dict[str, Any] = result.structuredContent or json.loads(result.content[0].text)  # type: ignore[union-attr]
    if tool == "list_devices":
        devices = ", ".join(f"{d['serial']} ({d['state']})" for d in data["devices"])
        return f"online={data['online']}  [{devices}]"
    if tool == "install_apk":
        return f"installed={data['installed']} {data['apk']} sha256={data['sha256'][:12]}..."
    if tool == "launch_app":
        state, ms = data["launch_state"], data["total_time_ms"]
        return f"{data['component']} status={data['status']} {state} {ms} ms"
    if tool == "dump_ui":
        labels = []

        def walk(nodes: list[dict[str, Any]]) -> None:
            for node in nodes:
                label = node.get("text") or node.get("content_desc")
                if label and ("clickable" in node.get("flags", []) or node["class"] == "TextView"):
                    labels.append(f"{node['class']}:{label!r}")
                walk(node.get("children", []))

        walk(data["nodes"])
        return f"{data['node_count']} nodes; " + ", ".join(labels[:6])
    if tool == "tap":
        matched = data.get("matched") or {}
        what = (
            matched.get("resource_id") or matched.get("text") or matched.get("content_desc") or "-"
        )
        return f"tapped ({data['x']}, {data['y']}) on {what}"
    if tool == "logcat":
        if data.get("note"):
            return str(data["note"])
        lines = [f"{e['level']}/{e['tag']}: {e['msg'][:90]}" for e in data["entries"]]
        return f"{data['returned']} lines\n" + "\n".join("        " + ln for ln in lines[:4])
    if tool == "screenshot":
        path = Path(data["path"]).relative_to(ROOT).as_posix()
        return f"saved {path} ({data['size_bytes']} bytes)"
    if tool == "run_test_suite":
        return (
            f"exit={data['exit_code']} passed={data['passed']} junit={data['junit']} "
            f"in {data['duration_s']}s"
        )
    return json.dumps({k: v for k, v in data.items() if k != "serial"})


async def main() -> None:
    shutil.rmtree(ARTIFACTS, ignore_errors=True)
    params = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "qa_mcp_server",
            "serve",
            "--fake",
            "--config",
            str(ROOT / "examples" / "qa-mcp.example.toml"),
            "--apk-dir",
            str(ROOT / "examples" / "apks"),
            "--artifacts-dir",
            str(ARTIFACTS),
        ],
        env=get_default_environment(),
        cwd=ROOT,
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        init = await session.initialize()
        tools = (await session.list_tools()).tools
        print(f"server {init.serverInfo.name} {init.serverInfo.version} (stdio, fake device)")
        print(f"{len(tools)} tools: {', '.join(t.name for t in tools)}\n")

        step = 0

        async def call(tool: str, args: dict[str, Any] | None = None, note: str = "") -> None:
            nonlocal step
            step += 1
            start = time.perf_counter()
            result = await session.call_tool(tool, args or {})
            ms = (time.perf_counter() - start) * 1000
            shown = ", ".join(f"{k}={v!r}" for k, v in (args or {}).items())
            if note:
                print(f"  # {note}")
            print(f"#{step:<2} {tool}({shown})  [{ms:.1f} ms]")
            print(f"    -> {summarize(tool, result)}")

        print("== exploratory session ==")
        await call("list_devices")
        await call("install_apk", {"apk_path": "sample-notes-1.0.apk"})
        await call("launch_app", {"package": APP})
        await call("dump_ui", note="read the screen before acting")
        await call("tap", {"content_desc": "New note"})
        await call("tap", {"resource_id": "title_input"})
        await call("input_text", {"text": "Groceries"})
        await call("tap", {"text": "Save"})
        await call("dump_ui", note="verify the note is listed")
        await call("logcat", {"package": APP, "level": "W"})
        await call("tap", {"content_desc": "New note"})
        await call("tap", {"content_desc": "Export as PDF"}, note="risky action")
        await call("dump_ui", note="what happened?")
        await call("logcat", {"tag": "AndroidRuntime", "level": "E"})
        await call("screenshot", {"save": True}, note="evidence for the bug report")
        await call("tap", {"text": "Close app"})
        await call("run_test_suite", {"name": "smoke"})

        print("\n== a prompt-injected agent tries to escape ==")
        await call("input_text", {"text": "hi; reboot"})
        await call("launch_app", {"package": APP, "activity": ".Main$(id)"})
        await call("install_apk", {"apk_path": "../../pyproject.toml"})
        await call("press_key", {"key": "POWER"})
        await call("run_test_suite", {"name": "smoke; rm -rf /"})
        await call("logcat", {"serial": "emulator-5554 && reboot"})

    records = [
        json.loads(line)
        for line in (ARTIFACTS / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    outcomes = Counter(r["outcome"] for r in records)
    executed = sum(1 for r in records for c in r["commands"] if c["executed"])
    log = (ARTIFACTS / "audit.jsonl").relative_to(ROOT).as_posix()
    print(
        f"\naudit log: {len(records)} tool calls {dict(outcomes)}, "
        f"{executed} adb/suite commands executed -> {log}"
    )


if __name__ == "__main__":
    anyio.run(main)
