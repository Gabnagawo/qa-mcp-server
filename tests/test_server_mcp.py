"""End-to-end tests through a real MCP client session (in-process memory transport)."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest
from mcp import ClientSession
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import CallToolResult, ImageContent, TextContent

from qa_mcp_server.audit import AuditLogger
from qa_mcp_server.config import Settings
from qa_mcp_server.fake import FakeAdbTransport
from qa_mcp_server.server import TOOL_NAMES, build_server, build_service

from .conftest import APP

pytestmark = pytest.mark.anyio


def data(result: CallToolResult) -> dict[str, Any]:
    assert not result.isError, result.content
    assert result.structuredContent is not None
    return result.structuredContent


async def test_lists_all_tools_with_schemas_and_hints(client: ClientSession) -> None:
    tools = {t.name: t for t in (await client.list_tools()).tools}
    assert sorted(tools) == sorted(TOOL_NAMES)
    assert len(tools) == 10
    assert tools["list_devices"].annotations is not None
    assert tools["list_devices"].annotations.readOnlyHint is True
    assert tools["install_apk"].annotations is not None
    assert tools["install_apk"].annotations.destructiveHint is True
    serial = tools["tap"].inputSchema["properties"]["serial"]
    assert "pattern" in json.dumps(serial)
    for tool in tools.values():
        assert tool.description


async def test_full_exploratory_flow(
    client: ClientSession, audit: AuditLogger, fake: FakeAdbTransport
) -> None:
    devices = data(await client.call_tool("list_devices", {}))
    assert devices["online"] == 1

    installed = data(await client.call_tool("install_apk", {"apk_path": "sample-notes-1.0.apk"}))
    assert installed["installed"] is True

    launched = data(await client.call_tool("launch_app", {"package": APP}))
    assert launched["launch_state"] == "COLD"

    ui = data(await client.call_tool("dump_ui", {}))
    assert ui["packages"] == [APP]

    tapped = data(await client.call_tool("tap", {"content_desc": "New note"}))
    assert tapped["matched"]["resource_id"].endswith("fab_new_note")

    data(await client.call_tool("tap", {"resource_id": "title_input"}))
    data(await client.call_tool("input_text", {"text": "Groceries"}))
    data(await client.call_tool("tap", {"text": "save", "match": "contains"}))
    after = json.dumps(data(await client.call_tool("dump_ui", {})))
    assert "Groceries" in after
    assert "Note saved" in after

    data(await client.call_tool("tap", {"content_desc": "New note"}))
    data(await client.call_tool("tap", {"content_desc": "Export as PDF"}))
    errors = data(await client.call_tool("logcat", {"level": "E", "package": APP}))
    assert errors["note"]  # process died: pid lookup fails after the crash
    crash = data(await client.call_tool("logcat", {"level": "E", "tag": "AndroidRuntime"}))
    assert any("FATAL EXCEPTION" in e["msg"] for e in crash["entries"])

    data(await client.call_tool("tap", {"text": "Close app"}))
    key = data(await client.call_tool("press_key", {"key": "home"}))
    assert key["detail"] == "KEYCODE_HOME"
    tapped_xy = data(await client.call_tool("tap", {"x": 540, "y": 1200}))
    assert tapped_xy["matched"] is None

    records = audit.read()
    assert len(records) == 16
    assert all(r["outcome"] == "ok" for r in records)
    typed = next(r for r in records if r["tool"] == "input_text")
    assert typed["arguments"]["text"] == "<redacted 9 chars>"
    launch = next(r for r in records if r["tool"] == "launch_app")
    assert [c["rule"] for c in launch["commands"]] == [
        "devices",
        "pm_path",
        "resolve_launcher",
        "am_start",
    ]
    assert all(c["executed"] for c in launch["commands"])


async def test_screenshot_returns_png_image(client: ClientSession) -> None:
    result = await client.call_tool("screenshot", {})
    assert not result.isError
    image = next(b for b in result.content if isinstance(b, ImageContent))
    assert image.mimeType == "image/png"
    assert base64.b64decode(image.data).startswith(b"\x89PNG")


async def test_screenshot_save_returns_path(client: ClientSession) -> None:
    result = await client.call_tool("screenshot", {"save": True})
    text = next(b for b in result.content if isinstance(b, TextContent)).text
    saved = json.loads(text)
    assert Path(saved["path"]).is_file()
    assert saved["size_bytes"] > 0


async def test_operational_error_is_audited_as_error(
    client: ClientSession, audit: AuditLogger
) -> None:
    result = await client.call_tool("launch_app", {"package": APP})
    assert result.isError
    assert audit.read()[-1]["outcome"] == "error"


async def test_long_arguments_are_truncated_in_audit(
    client: ClientSession, audit: AuditLogger
) -> None:
    await client.call_tool("tap", {"text": "z" * 190 + "é" * 10})
    await client.call_tool("logcat", {"tag": "x" * 400})
    assert audit.read()[-1]["arguments"]["tag"].endswith("<400 chars>")


async def test_dry_run_plans_but_does_not_execute(tmp_path: Path, apk_dir: Path) -> None:
    settings = Settings(
        backend="fake", apk_dir=apk_dir, artifacts_dir=tmp_path / "out", dry_run=True
    )
    fake = FakeAdbTransport.from_file()
    server = build_server(settings, service=build_service(settings, transport=fake))
    async with create_connected_server_and_client_session(server) as session:
        result = data(await session.call_tool("install_apk", {"apk_path": "sample-notes-1.0.apk"}))
        assert result == result | {"installed": False, "dry_run": True}
        data(await session.call_tool("tap", {"text": "Settings"}))
        data(await session.call_tool("input_text", {"text": "hello"}))
    assert APP not in fake.state.installed
    assert not any("input" in call for call in fake.calls)
    records = server.audit.read()
    assert all(r["dry_run"] for r in records)
    tap_cmds = records[1]["commands"]
    assert [(c["rule"], c["executed"]) for c in tap_cmds] == [
        ("devices", True),
        ("ui_dump", True),
        ("tap", False),
    ]


async def test_unknown_tool_is_rejected(client: ClientSession, audit: AuditLogger) -> None:
    result = await client.call_tool("reboot_device", {})
    assert result.isError
    assert audit.read()[-1]["outcome"] == "rejected"


async def test_audit_record_complex_argument_values(audit: AuditLogger) -> None:
    from qa_mcp_server.audit import sanitize_arguments

    clean = sanitize_arguments(
        "tap", {"nested": {"a": 1}, "big": ["x" * 400]}, redact_input_text=True
    )
    assert clean["nested"] == {"a": 1}
    assert clean["big"].endswith("...")
    assert AuditLogger(audit.path.parent / "missing" / "x.jsonl").read() == []
