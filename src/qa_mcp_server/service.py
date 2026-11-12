"""Tool implementations, independent of the MCP transport.

Every public method takes an already validated request model, applies the
policies that need runtime context (device attached, package allowlist, APK
sandbox) and returns a pydantic result model.
"""

from __future__ import annotations

import hashlib
import re
import sys
import time
from datetime import UTC, datetime
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

from defusedxml import ElementTree as SafeET
from pydantic import BaseModel

from qa_mcp_server.adb import AdbClient
from qa_mcp_server.audit import record_command
from qa_mcp_server.config import Settings, TestSuiteConfig
from qa_mcp_server.errors import CommandFailed, DeviceError, PolicyViolation
from qa_mcp_server.transport import Transport
from qa_mcp_server.uitree import find_nodes, parse_hierarchy, to_tree
from qa_mcp_server.validation import (
    SERIAL_RE,
    DumpUiRequest,
    InputTextRequest,
    InstallRequest,
    LaunchRequest,
    LogcatRequest,
    PressKeyRequest,
    RunSuiteRequest,
    ScreenshotRequest,
    TapRequest,
    level_at_least,
    resolve_apk_path,
)

# A suite argv entry equal to this is replaced by the server's own interpreter.
PYTHON_PLACEHOLDER = "{python}"

_THREADTIME_RE = re.compile(
    r"^(?P<ts>\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3})\s+(?P<pid>\d+)\s+(?P<tid>\d+)\s+"
    r"(?P<level>[VDIWEF])\s+(?P<tag>[^:]*?)\s*: (?P<msg>.*)$"
)


# -- result models -----------------------------------------------------------------


class DeviceInfo(BaseModel):
    serial: str
    state: str
    model: str | None = None
    product: str | None = None


class DevicesResult(BaseModel):
    devices: list[DeviceInfo]
    online: int


class InstallResult(BaseModel):
    serial: str
    apk: str
    sha256: str
    size_bytes: int
    installed: bool
    dry_run: bool


class LaunchResult(BaseModel):
    serial: str
    component: str
    status: str
    launch_state: str | None = None
    total_time_ms: int | None = None
    dry_run: bool


class LogEntry(BaseModel):
    ts: str
    pid: int
    tid: int
    level: str
    tag: str
    msg: str


class LogcatResult(BaseModel):
    serial: str
    entries: list[LogEntry]
    returned: int
    scanned: int
    truncated: bool
    note: str | None = None


class TapResult(BaseModel):
    serial: str
    x: int
    y: int
    matched: dict[str, Any] | None = None
    match_count: int | None = None
    dry_run: bool


class ActionResult(BaseModel):
    serial: str
    action: str
    detail: str
    dry_run: bool


class SuiteResult(BaseModel):
    name: str
    exit_code: int | None
    passed: bool | None
    duration_s: float
    output_tail: str
    truncated: bool
    junit: dict[str, int] | None = None
    dry_run: bool


class SavedScreenshot(BaseModel):
    serial: str
    path: str
    size_bytes: int
    sha256: str


class QAService:
    def __init__(self, settings: Settings, adb: AdbClient, suite_transport: Transport) -> None:
        self.settings = settings
        self.adb = adb
        self.suite_transport = suite_transport

    # -- helpers -------------------------------------------------------------------

    def resolve_serial(self, serial: str | None) -> str:
        """Return a validated serial of an attached, online device."""
        if serial is not None and not re.fullmatch(SERIAL_RE, serial):
            raise PolicyViolation("invalid device serial")
        online = [d.serial for d in self.adb.devices() if d.state == "device"]
        if serial is None:
            if len(online) == 1:
                return online[0]
            if not online:
                raise DeviceError("no online device; run list_devices")
            raise DeviceError(f"several devices online, pass serial (one of {online})")
        if serial not in online:
            raise DeviceError(f"device {serial!r} is not attached or not online")
        return serial

    def check_package(self, package: str) -> None:
        patterns = self.settings.allowed_packages
        if not patterns:
            return
        for pattern in patterns:
            if fnmatchcase(package, pattern) or package == pattern.removesuffix(".*"):
                return
        raise PolicyViolation(f"package {package!r} is not in allowed_packages")

    # -- tools -----------------------------------------------------------------------

    def list_devices(self) -> DevicesResult:
        devices = self.adb.devices()
        return DevicesResult(
            devices=[
                DeviceInfo(serial=d.serial, state=d.state, model=d.model, product=d.product)
                for d in devices
            ],
            online=sum(1 for d in devices if d.state == "device"),
        )

    def install_apk(self, req: InstallRequest) -> InstallResult:
        apk = resolve_apk_path(req.apk_path, self.settings.apk_dir, self.settings.max_apk_bytes)
        serial = self.resolve_serial(req.serial)
        result = self.adb.install(serial, str(apk))
        data = apk.read_bytes()
        return InstallResult(
            serial=serial,
            apk=apk.name,
            sha256=hashlib.sha256(data).hexdigest(),
            size_bytes=len(data),
            installed=not result.dry_run,
            dry_run=result.dry_run,
        )

    def launch_app(self, req: LaunchRequest) -> LaunchResult:
        self.check_package(req.package)
        serial = self.resolve_serial(req.serial)
        if not self.adb.is_installed(serial, req.package):
            raise CommandFailed(f"package {req.package!r} is not installed on {serial}")
        component = req.component or self.adb.resolve_launcher(serial, req.package)
        if component is None:
            raise CommandFailed(f"no launcher activity found for {req.package!r}; pass activity")
        if not component.startswith(req.package + "/"):
            raise PolicyViolation("resolved activity belongs to another package")
        outcome = self.adb.start_activity(serial, component)
        return LaunchResult(
            serial=serial,
            component=outcome.component,
            status=outcome.status,
            launch_state=outcome.launch_state,
            total_time_ms=outcome.total_time_ms,
            dry_run=outcome.dry_run,
        )

    def screenshot(self, req: ScreenshotRequest) -> tuple[bytes, SavedScreenshot | None]:
        serial = self.resolve_serial(req.serial)
        png = self.adb.screencap(serial)
        if not req.save:
            return png, None
        out_dir = self.settings.artifacts_dir / "screenshots"
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        path = out_dir / f"{serial.replace(':', '_')}-{stamp}.png"
        path.write_bytes(png)
        return png, SavedScreenshot(
            serial=serial,
            path=str(path),
            size_bytes=len(png),
            sha256=hashlib.sha256(png).hexdigest(),
        )

    def logcat(self, req: LogcatRequest) -> LogcatResult:
        serial = self.resolve_serial(req.serial)
        limit = min(
            req.max_lines or self.settings.logcat_default_lines, self.settings.logcat_max_lines
        )
        pid = None
        if req.package is not None:
            self.check_package(req.package)
            pid = self.adb.pidof(serial, req.package)
            if pid is None:
                return LogcatResult(
                    serial=serial,
                    entries=[],
                    returned=0,
                    scanned=0,
                    truncated=False,
                    note=f"{req.package} is not running; start it or omit package",
                )
        # Read more than requested so tag/level/since filters still have lines to work on.
        scan = min(limit * 5, self.settings.logcat_max_lines * 5)
        result = self.adb.logcat(serial, scan, pid)
        # threadtime has no year: compare "MM-DD HH:MM:SS.mmm" strings.
        since = req.since[5:] if req.since and re.match(r"^\d{4}-", req.since) else req.since
        entries: list[LogEntry] = []
        scanned = 0
        for line in result.text.splitlines():
            match = _THREADTIME_RE.match(line)
            if not match:
                continue
            scanned += 1
            entry = LogEntry(
                ts=match["ts"],
                pid=int(match["pid"]),
                tid=int(match["tid"]),
                level=match["level"],
                tag=match["tag"].strip(),
                msg=match["msg"][:2000],
            )
            if req.tag is not None and entry.tag != req.tag:
                continue
            if not level_at_least(entry.level, req.level):
                continue
            if since is not None and entry.ts < since:
                continue
            entries.append(entry)
        truncated = result.truncated or len(entries) > limit
        entries = entries[-limit:]
        return LogcatResult(
            serial=serial,
            entries=entries,
            returned=len(entries),
            scanned=scanned,
            truncated=truncated,
        )

    def dump_ui(self, req: DumpUiRequest) -> dict[str, Any]:
        serial = self.resolve_serial(req.serial)
        nodes = parse_hierarchy(self.adb.ui_dump(serial))
        tree = to_tree(nodes, compact=req.compact, max_nodes=self.settings.ui_max_nodes)
        return {"serial": serial, **tree}

    def tap(self, req: TapRequest) -> TapResult:
        serial = self.resolve_serial(req.serial)
        if req.selector is None:
            assert req.x is not None  # noqa: S101 (guaranteed by TapRequest validator)
            assert req.y is not None  # noqa: S101
            result = self.adb.tap(serial, req.x, req.y)
            return TapResult(serial=serial, x=req.x, y=req.y, dry_run=result.dry_run)
        nodes = parse_hierarchy(self.adb.ui_dump(serial))
        matches = find_nodes(nodes, req.selector)
        if not matches:
            raise CommandFailed(
                f"no element with {req.selector.field}={req.selector.value!r} on screen; "
                "call dump_ui to see what is there"
            )
        if req.selector.index >= len(matches):
            raise CommandFailed(f"selector matched {len(matches)} element(s); index out of range")
        node = matches[req.selector.index]
        center = node.center
        assert center is not None  # noqa: S101 (find_nodes only returns nodes with bounds)
        x, y = center
        result = self.adb.tap(serial, x, y)
        return TapResult(
            serial=serial,
            x=x,
            y=y,
            matched=node.to_dict([]),
            match_count=len(matches),
            dry_run=result.dry_run,
        )

    def input_text(self, req: InputTextRequest) -> ActionResult:
        serial = self.resolve_serial(req.serial)
        result = self.adb.input_text(serial, req.encoded)
        return ActionResult(
            serial=serial,
            action="input_text",
            detail=f"typed {len(req.text)} characters",
            dry_run=result.dry_run,
        )

    def press_key(self, req: PressKeyRequest) -> ActionResult:
        serial = self.resolve_serial(req.serial)
        result = self.adb.keyevent(serial, req.keycode)
        return ActionResult(
            serial=serial, action="press_key", detail=req.keycode, dry_run=result.dry_run
        )

    def run_test_suite(self, req: RunSuiteRequest) -> SuiteResult:
        suite = self.settings.test_suites.get(req.name)
        if suite is None:
            names = sorted(self.settings.test_suites) or ["<none configured>"]
            raise PolicyViolation(f"unknown test suite {req.name!r}; configured: {names}")
        env = dict(suite.env)
        if req.serial is not None:
            env["ANDROID_SERIAL"] = self.resolve_serial(req.serial)
        argv = tuple(sys.executable if arg == PYTHON_PLACEHOLDER else arg for arg in suite.argv)
        record_command(f"test_suite:{req.name}", argv, executed=not self.settings.dry_run)
        if self.settings.dry_run:
            return SuiteResult(
                name=req.name,
                exit_code=None,
                passed=None,
                duration_s=0.0,
                output_tail="",
                truncated=False,
                dry_run=True,
            )
        start = time.monotonic()
        result = self.suite_transport.run(
            argv,
            timeout_s=suite.timeout_s,
            max_bytes=self.settings.max_output_bytes,
            cwd=suite.cwd,
            env=env,
        )
        output = (result.text + ("\n" + result.error_text if result.error_text else "")).strip()
        tail = output.encode()[-self.settings.suite_output_tail_bytes :].decode("utf-8", "replace")
        return SuiteResult(
            name=req.name,
            exit_code=result.returncode,
            passed=result.returncode == 0,
            duration_s=round(time.monotonic() - start, 3),
            output_tail=tail,
            truncated=result.truncated or len(output.encode()) > len(tail.encode()),
            junit=_read_junit(suite),
            dry_run=False,
        )


def _read_junit(suite: TestSuiteConfig) -> dict[str, int] | None:
    if suite.junit_xml is None:
        return None
    base = (suite.cwd or Path.cwd()).resolve()
    path = (base / suite.junit_xml).resolve()
    if not path.is_relative_to(base) or not path.is_file():
        return None
    try:
        root = SafeET.parse(path).getroot()
    except Exception:
        return None
    if root is None:
        return None
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    totals = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    for element in suites:
        for key in totals:
            totals[key] += int(element.get(key, "0") or 0)
    return totals
