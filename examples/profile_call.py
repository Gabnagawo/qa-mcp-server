"""Profile where the time of a tool call goes (fake device, in-process transport).

uv run python examples/profile_call.py
"""

from __future__ import annotations

import cProfile
import pstats
import tempfile
from pathlib import Path
from typing import Any

import anyio
from mcp.shared.memory import create_connected_server_and_client_session

from qa_mcp_server.config import Settings
from qa_mcp_server.server import build_server

ROOT = Path(__file__).resolve().parents[1]
CALLS = 100


async def main() -> None:
    settings = Settings(
        backend="fake", apk_dir=ROOT / "examples" / "apks", artifacts_dir=Path(tempfile.mkdtemp())
    )
    async with create_connected_server_and_client_session(build_server(settings)) as client:
        await client.call_tool("list_devices", {})  # warm-up
        profiler = cProfile.Profile()
        profiler.enable()
        for _ in range(CALLS):
            await client.call_tool("list_devices", {})
        profiler.disable()

    stats = pstats.Stats(profiler)
    table: dict[Any, Any] = stats.stats  # type: ignore[attr-defined]
    total = sum(row[2] for row in table.values())

    def self_time(*needles: str) -> float:
        return float(sum(row[2] for key, row in table.items() if any(n in key[0] for n in needles)))

    schema = self_time("jsonschema", "referencing", "rpds")
    ours = self_time("qa_mcp_server")
    print(f"{CALLS} list_devices calls, in-process, under cProfile: self-time {total:.3f}s")
    print(f"  jsonschema/referencing (MCP SDK output-schema validation): {schema:.3f}s")
    print(f"    = {schema / total:.0%} of the total")
    print(f"  qa_mcp_server code (validation, policy, fake adb, parsing, audit): {ours:.3f}s")
    print(f"    = {ours / total:.1%} of the total")


if __name__ == "__main__":
    anyio.run(main)
