"""FastMCP server: tool registration and the audit wrapper around every call."""

from __future__ import annotations

import base64
import json
import time
from collections.abc import Callable, Sequence
from typing import Annotated, Any, Literal

import anyio.to_thread
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ContentBlock, ImageContent, TextContent, ToolAnnotations
from pydantic import Field, ValidationError

from qa_mcp_server import __version__
from qa_mcp_server.adb import AdbClient, AdbLimits
from qa_mcp_server.audit import AuditLogger, capture_commands, sanitize_arguments
from qa_mcp_server.config import Settings
from qa_mcp_server.errors import PolicyViolation
from qa_mcp_server.fake import FakeAdbTransport
from qa_mcp_server.service import (
    ActionResult,
    DevicesResult,
    InstallResult,
    LaunchResult,
    LogcatResult,
    QAService,
    SuiteResult,
    TapResult,
)
from qa_mcp_server.transport import SubprocessTransport, Transport
from qa_mcp_server.validation import (
    ActivityName,
    DumpUiRequest,
    InputTextRequest,
    InstallRequest,
    LaunchRequest,
    LogcatRequest,
    LogLevel,
    PackageName,
    PressKeyRequest,
    RunSuiteRequest,
    ScreenshotRequest,
    Selector,
    Serial,
    TapRequest,
)

INSTRUCTIONS = """\
Android QA tools over adb. Typical exploratory loop: list_devices -> launch_app ->
dump_ui (read the screen) -> tap / input_text / press_key -> dump_ui again -> logcat
(level E, package) to catch crashes -> screenshot for evidence. Prefer tap with a
selector (text, resource_id or content_desc taken from dump_ui) over raw coordinates.
All inputs are validated; commands outside the allowlist are rejected."""

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
DEVICE_ACTION = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)
INSTALL = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False)


def _classify(exc: BaseException) -> str:
    cause = exc.__cause__ or exc
    if isinstance(cause, PolicyViolation | ValidationError):
        return "rejected"
    if isinstance(exc, ToolError) and exc.__cause__ is None and str(exc).startswith("Unknown tool"):
        return "rejected"
    return "error"


class AuditedFastMCP(FastMCP):
    """FastMCP that writes one audit record per ``tools/call``, including rejected ones.

    Overriding ``call_tool`` (rather than wrapping each function) means argument
    validation failures, unknown tools and policy rejections are all audited.
    """

    def __init__(self, *args: Any, audit: AuditLogger, settings: Settings, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.audit = audit
        self.qa_settings = settings

    async def call_tool(
        self, name: str, arguments: dict[str, Any]
    ) -> Sequence[ContentBlock] | dict[str, Any]:
        start = time.perf_counter()
        clean = sanitize_arguments(
            name, arguments or {}, redact_input_text=self.qa_settings.redact_input_text
        )
        with capture_commands() as commands:
            try:
                result = await super().call_tool(name, arguments)
            except Exception as exc:
                self.audit.write(
                    tool=name,
                    arguments=clean,
                    outcome=_classify(exc),
                    duration_ms=(time.perf_counter() - start) * 1000,
                    dry_run=self.qa_settings.dry_run,
                    commands=list(commands),
                    error=str(exc),
                )
                raise
        self.audit.write(
            tool=name,
            arguments=clean,
            outcome="ok",
            duration_ms=(time.perf_counter() - start) * 1000,
            dry_run=self.qa_settings.dry_run,
            commands=list(commands),
        )
        return result


async def _offload[T](fn: Callable[[], T]) -> T:
    """Run blocking adb work in a worker thread so the event loop stays responsive."""
    return await anyio.to_thread.run_sync(fn)


def build_transport(settings: Settings) -> Transport:
    if settings.backend == "fake":
        return FakeAdbTransport.from_file(settings.fake_scenario)
    return SubprocessTransport()


def build_service(settings: Settings, transport: Transport | None = None) -> QAService:
    adb = AdbClient(
        transport or build_transport(settings),
        adb_path=settings.adb_path,
        limits=AdbLimits(
            command_timeout_s=settings.command_timeout_s,
            install_timeout_s=settings.install_timeout_s,
            max_output_bytes=settings.max_output_bytes,
            max_screenshot_bytes=settings.max_screenshot_bytes,
            logcat_max_bytes=settings.logcat_max_bytes,
        ),
        dry_run=settings.dry_run,
    )
    return QAService(settings, adb, SubprocessTransport())


def build_server(
    settings: Settings,
    *,
    service: QAService | None = None,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> AuditedFastMCP:
    """Create the MCP server with all tools registered."""
    svc = service or build_service(settings)
    mcp = AuditedFastMCP(
        "qa-mcp-server",
        instructions=INSTRUCTIONS,
        audit=AuditLogger(settings.audit_log_path),
        settings=settings,
        host=host,
        port=port,
        log_level="WARNING",
    )
    mcp._mcp_server.version = __version__

    @mcp.tool(annotations=READ_ONLY)
    async def list_devices() -> DevicesResult:
        """List attached Android devices and emulators with their state.

        State is device, offline or unauthorized; only "device" accepts commands."""
        return await _offload(svc.list_devices)

    @mcp.tool(annotations=INSTALL)
    async def install_apk(
        apk_path: Annotated[
            str,
            Field(
                min_length=1,
                max_length=4096,
                description="File name or path of an .apk inside the server's APK directory.",
            ),
        ],
        serial: Serial | None = None,
    ) -> InstallResult:
        """Install (or reinstall, keeping data) an APK from the allowed APK directory."""
        req = InstallRequest(apk_path=apk_path, serial=serial)
        return await _offload(lambda: svc.install_apk(req))

    @mcp.tool(annotations=DEVICE_ACTION)
    async def launch_app(
        package: PackageName,
        activity: ActivityName | None = None,
        serial: Serial | None = None,
    ) -> LaunchResult:
        """Start an installed app (launcher activity unless `activity` is given).

        Reports launch state (COLD/WARM/HOT) and TotalTime from `am start -W`."""
        req = LaunchRequest(package=package, activity=activity, serial=serial)
        return await _offload(lambda: svc.launch_app(req))

    @mcp.tool(annotations=READ_ONLY, structured_output=False)
    async def screenshot(
        serial: Serial | None = None,
        save: Annotated[
            bool,
            Field(
                description="Save a PNG under the artifacts dir and return its path, not the image."
            ),
        ] = False,
    ) -> list[ContentBlock]:
        """Capture the screen as PNG image content, or save it and return the file path."""
        req = ScreenshotRequest(serial=serial, save=save)
        png, saved = await _offload(lambda: svc.screenshot(req))
        if saved is not None:
            return [TextContent(type="text", text=saved.model_dump_json())]
        return [
            ImageContent(type="image", data=base64.b64encode(png).decode(), mimeType="image/png"),
            TextContent(type="text", text=json.dumps({"size_bytes": len(png)})),
        ]

    @mcp.tool(annotations=READ_ONLY)
    async def logcat(
        serial: Serial | None = None,
        package: Annotated[
            PackageName | None, Field(description="Only lines from this app's running process.")
        ] = None,
        tag: Annotated[str | None, Field(pattern=r"^[A-Za-z0-9_.\-]{1,64}$")] = None,
        level: Annotated[
            LogLevel, Field(description="Minimum priority: V < D < I < W < E < F.")
        ] = "V",
        since: Annotated[
            str | None,
            Field(
                pattern=r"^(?:\d{4}-)?\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d{1,3})?$",
                description="Only lines at or after '[YYYY-]MM-DD HH:MM:SS[.mmm]' (device time).",
            ),
        ] = None,
        max_lines: Annotated[int | None, Field(ge=1, le=5000)] = None,
    ) -> LogcatResult:
        """Read the logcat buffer (non-blocking dump), size-capped.

        Filters: package (running process), tag, minimum level and since."""
        req = LogcatRequest(
            serial=serial, package=package, tag=tag, level=level, since=since, max_lines=max_lines
        )
        return await _offload(lambda: svc.logcat(req))

    @mcp.tool(annotations=READ_ONLY)
    async def dump_ui(
        serial: Serial | None = None,
        compact: Annotated[
            bool, Field(description="Drop layout-only wrapper nodes (recommended).")
        ] = True,
    ) -> dict[str, Any]:
        """Dump the current screen's UI hierarchy (uiautomator) as a JSON tree: class, text,
        resource_id, content_desc, bounds [x1,y1,x2,y2] and flags (clickable, scrollable, ...)."""
        req = DumpUiRequest(serial=serial, compact=compact)
        return await _offload(lambda: svc.dump_ui(req))

    @mcp.tool(annotations=DEVICE_ACTION)
    async def tap(
        x: Annotated[int | None, Field(ge=0, le=10000)] = None,
        y: Annotated[int | None, Field(ge=0, le=10000)] = None,
        text: Annotated[
            str | None, Field(max_length=200, description="Selector: visible text")
        ] = None,
        resource_id: Annotated[
            str | None,
            Field(
                max_length=255, description="Selector: resource-id, full or short ('save_button')"
            ),
        ] = None,
        content_desc: Annotated[
            str | None, Field(max_length=200, description="Selector: content description")
        ] = None,
        match: Literal["exact", "contains"] = "exact",
        index: Annotated[int, Field(ge=0, le=100, description="Which match to tap")] = 0,
        serial: Serial | None = None,
    ) -> TapResult:
        """Tap at (x, y), or on the element found by ONE selector (text, resource_id or
        content_desc), resolved through a fresh UI dump and tapped at its center."""
        selector = None
        if text is not None or resource_id is not None or content_desc is not None:
            selector = Selector(
                text=text,
                resource_id=resource_id,
                content_desc=content_desc,
                match=match,
                index=index,
            )
        req = TapRequest(x=x, y=y, selector=selector, serial=serial)
        return await _offload(lambda: svc.tap(req))

    @mcp.tool(annotations=DEVICE_ACTION)
    async def input_text(
        text: Annotated[
            str,
            Field(
                min_length=1,
                max_length=500,
                description="ASCII letters, digits, space and @._,:/+=- only (device-shell safe).",
            ),
        ],
        serial: Serial | None = None,
    ) -> ActionResult:
        """Type text into the focused field (tap the field first)."""
        req = InputTextRequest(text=text, serial=serial)
        return await _offload(lambda: svc.input_text(req))

    @mcp.tool(annotations=DEVICE_ACTION)
    async def press_key(
        key: Annotated[
            str,
            Field(description="Key name, e.g. BACK, HOME, ENTER, DEL, TAB, DPAD_DOWN, APP_SWITCH."),
        ],
        serial: Serial | None = None,
    ) -> ActionResult:
        """Send one allowlisted key event."""
        req = PressKeyRequest(key=key, serial=serial)
        return await _offload(lambda: svc.press_key(req))

    @mcp.tool(annotations=DEVICE_ACTION)
    async def run_test_suite(
        name: Annotated[
            str, Field(description="Name of a suite configured by the server operator.")
        ],
        serial: Serial | None = None,
    ) -> SuiteResult:
        """Run a pre-configured test suite by name (exported ANDROID_SERIAL = serial).
        Only suites declared in the server config can run; arguments cannot be changed."""
        req = RunSuiteRequest(name=name, serial=serial)
        return await _offload(lambda: svc.run_test_suite(req))

    return mcp


TOOL_NAMES = (
    "list_devices",
    "install_apk",
    "launch_app",
    "screenshot",
    "logcat",
    "dump_ui",
    "tap",
    "input_text",
    "press_key",
    "run_test_suite",
)
