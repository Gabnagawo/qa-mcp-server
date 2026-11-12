"""Measure tool-call latency against the fake device.

Every call goes through a real MCP client session: argument validation, the
policy check, the fake adb transport, output parsing, result serialisation and
the audit-log write. Two transports are measured:

* ``memory`` - in-process streams (server overhead only);
* ``stdio``  - a server subprocess, as Claude Desktop / Claude Code run it.

    uv run python examples/benchmark_latency.py [--iterations 200]
"""

from __future__ import annotations

import argparse
import platform
import statistics
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import get_default_environment, stdio_client
from mcp.shared.memory import create_connected_server_and_client_session

from qa_mcp_server import __version__
from qa_mcp_server.config import Settings
from qa_mcp_server.server import build_server

ROOT = Path(__file__).resolve().parents[1]
APK_DIR = ROOT / "examples" / "apks"
APP = "org.qamcp.sample.notes"

# (tool, arguments). The app is installed and running before measuring.
CALLS: list[tuple[str, dict[str, Any]]] = [
    ("list_devices", {}),
    ("install_apk", {"apk_path": "sample-notes-1.0.apk"}),
    ("launch_app", {"package": APP}),
    ("dump_ui", {}),
    ("tap", {"x": 540, "y": 1200}),
    ("tap", {"content_desc": "More options"}),
    ("input_text", {"text": "benchmark"}),
    ("press_key", {"key": "ENTER"}),
    ("logcat", {"level": "W"}),
    ("logcat", {"package": APP}),
    ("screenshot", {}),
]
REJECTED = ("input_text", {"text": "x; reboot"})


def percentile(samples: list[float], pct: float) -> float:
    ordered = sorted(samples)
    index = min(len(ordered) - 1, round(pct / 100 * (len(ordered) - 1)))
    return ordered[index]


async def measure(session: ClientSession, iterations: int) -> list[tuple[str, list[float]]]:
    await session.call_tool("install_apk", {"apk_path": "sample-notes-1.0.apk"})
    await session.call_tool("launch_app", {"package": APP})
    rows = []
    for tool, args in [*CALLS, REJECTED]:
        label = tool + ("(" + ",".join(args) + ")" if args else "()")
        if (tool, args) == REJECTED:
            label = "rejected input_text"
        samples = []
        for _ in range(iterations):
            start = time.perf_counter()
            result = await session.call_tool(tool, args)
            samples.append((time.perf_counter() - start) * 1000)
            if result.isError != ((tool, args) == REJECTED):
                raise RuntimeError(f"unexpected result for {label}: {result.content}")
        rows.append((label, samples))
    return rows


def report(transport: str, rows: list[tuple[str, list[float]]], iterations: int) -> None:
    print(f"\ntransport={transport} iterations={iterations} per tool (ms)")
    print(f"{'tool call':<32} {'p50':>7} {'p95':>7} {'mean':>7} {'max':>7}")
    everything: list[float] = []
    for label, samples in rows:
        everything.extend(samples)
        print(
            f"{label:<32} {statistics.median(samples):>7.2f} {percentile(samples, 95):>7.2f} "
            f"{statistics.fmean(samples):>7.2f} {max(samples):>7.2f}"
        )
    print(
        f"{'ALL (' + str(len(everything)) + ' calls)':<32} {statistics.median(everything):>7.2f} "
        f"{percentile(everything, 95):>7.2f} {statistics.fmean(everything):>7.2f} "
        f"{max(everything):>7.2f}"
    )


async def run_memory(iterations: int, artifacts: Path) -> list[tuple[str, list[float]]]:
    settings = Settings(backend="fake", apk_dir=APK_DIR, artifacts_dir=artifacts / "memory")
    async with create_connected_server_and_client_session(build_server(settings)) as session:
        return await measure(session, iterations)


async def run_stdio(iterations: int, artifacts: Path) -> list[tuple[str, list[float]]]:
    params = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "qa_mcp_server",
            "serve",
            "--fake",
            "--apk-dir",
            str(APK_DIR),
            "--artifacts-dir",
            str(artifacts / "stdio"),
        ],
        env=get_default_environment(),
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        return await measure(session, iterations)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=200)
    args = parser.parse_args()
    print(
        f"qa-mcp-server {__version__} | Python {platform.python_version()} | "
        f"{platform.system()} {platform.release()} | {platform.processor() or platform.machine()}"
    )
    runners: list[tuple[str, Callable[[int, Path], Awaitable[list[tuple[str, list[float]]]]]]] = [
        ("memory", run_memory),
        ("stdio", run_stdio),
    ]
    with tempfile.TemporaryDirectory() as tmp:
        for name, runner in runners:
            report(name, await runner(args.iterations, Path(tmp)), args.iterations)


if __name__ == "__main__":
    anyio.run(main)
