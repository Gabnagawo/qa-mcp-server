"""Typed adb client: builds argv lists, enforces the allowlist, parses output.

The client never touches ``subprocess`` itself; it hands validated argv lists
to a :class:`~qa_mcp_server.transport.Transport`. The same code path runs
against a real device (``SubprocessTransport``) and against the scripted fake
device (``FakeAdbTransport``), so tests exercise the real parsing and policy.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from qa_mcp_server.audit import record_command
from qa_mcp_server.errors import CommandFailed, OutputTooLarge
from qa_mcp_server.policy import validate_adb_args
from qa_mcp_server.transport import RunResult, Transport

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


@dataclass(frozen=True)
class Device:
    serial: str
    state: str
    model: str | None = None
    product: str | None = None
    transport_id: str | None = None


@dataclass(frozen=True)
class LaunchOutcome:
    component: str
    status: str
    launch_state: str | None
    total_time_ms: int | None
    dry_run: bool


@dataclass(frozen=True)
class AdbLimits:
    command_timeout_s: float = 30.0
    install_timeout_s: float = 180.0
    max_output_bytes: int = 4 * 1024 * 1024
    max_screenshot_bytes: int = 16 * 1024 * 1024
    logcat_max_bytes: int = 256 * 1024


class AdbClient:
    def __init__(
        self,
        transport: Transport,
        *,
        adb_path: str = "adb",
        limits: AdbLimits | None = None,
        dry_run: bool = False,
    ) -> None:
        self.transport = transport
        self.adb_path = adb_path
        self.limits = limits or AdbLimits()
        self.dry_run = dry_run

    # -- execution -----------------------------------------------------------------

    def _exec(
        self,
        args: list[str],
        *,
        timeout_s: float | None = None,
        max_bytes: int | None = None,
        check: bool = True,
    ) -> RunResult:
        rule = validate_adb_args(args)
        argv = (self.adb_path, *args)
        skip = self.dry_run and rule.mutating
        record_command(rule.name, argv, executed=not skip)
        if skip:
            return RunResult(argv, 0, b"", b"", 0.0, dry_run=True)
        result = self.transport.run(
            argv,
            timeout_s=timeout_s or self.limits.command_timeout_s,
            max_bytes=max_bytes or self.limits.max_output_bytes,
        )
        if check and result.returncode != 0:
            detail = result.error_text or result.text.strip()
            raise CommandFailed(
                f"adb {rule.name} failed (exit {result.returncode}): {detail[-500:]}"
            )
        return result

    # -- commands ------------------------------------------------------------------

    def devices(self) -> list[Device]:
        result = self._exec(["devices", "-l"])
        return parse_devices(result.text)

    def install(self, serial: str, apk_path: str) -> RunResult:
        result = self._exec(
            ["-s", serial, "install", "-r", apk_path], timeout_s=self.limits.install_timeout_s
        )
        if not result.dry_run and "Success" not in result.text:
            raise CommandFailed(f"install failed: {result.text.strip()[-500:]}")
        return result

    def is_installed(self, serial: str, package: str) -> bool:
        result = self._exec(["-s", serial, "shell", "pm", "path", package], check=False)
        return result.returncode == 0 and "package:" in result.text

    def resolve_launcher(self, serial: str, package: str) -> str | None:
        result = self._exec(
            [
                "-s",
                serial,
                "shell",
                "cmd",
                "package",
                "resolve-activity",
                "--brief",
                "-c",
                "android.intent.category.LAUNCHER",
                package,
            ],
            check=False,
        )
        lines = [ln.strip() for ln in result.text.splitlines() if ln.strip()]
        if result.returncode != 0 or not lines or "/" not in lines[-1]:
            return None
        return lines[-1]

    def start_activity(self, serial: str, component: str) -> LaunchOutcome:
        result = self._exec(["-s", serial, "shell", "am", "start", "-W", "-n", component])
        if result.dry_run:
            return LaunchOutcome(component, "planned", None, None, dry_run=True)
        return parse_am_start(component, result.text)

    def pidof(self, serial: str, package: str) -> int | None:
        result = self._exec(["-s", serial, "shell", "pidof", package], check=False)
        first = result.text.split()
        if result.returncode != 0 or not first or not first[0].isdigit():
            return None
        return int(first[0])

    def screencap(self, serial: str) -> bytes:
        result = self._exec(
            ["-s", serial, "exec-out", "screencap", "-p"],
            max_bytes=self.limits.max_screenshot_bytes,
        )
        if result.truncated:
            raise OutputTooLarge("screenshot exceeds max_screenshot_bytes")
        if not result.stdout.startswith(PNG_MAGIC):
            raise CommandFailed("screencap did not return a PNG")
        return result.stdout

    def ui_dump(self, serial: str) -> str:
        result = self._exec(["-s", serial, "exec-out", "uiautomator", "dump", "/dev/tty"])
        if result.truncated:
            raise OutputTooLarge("UI hierarchy exceeds max_output_bytes")
        return result.text

    def tap(self, serial: str, x: int, y: int) -> RunResult:
        return self._exec(["-s", serial, "shell", "input", "tap", str(x), str(y)])

    def input_text(self, serial: str, encoded_text: str) -> RunResult:
        return self._exec(["-s", serial, "shell", "input", "text", encoded_text])

    def keyevent(self, serial: str, keycode: str) -> RunResult:
        return self._exec(["-s", serial, "shell", "input", "keyevent", keycode])

    def logcat(self, serial: str, lines: int, pid: int | None = None) -> RunResult:
        args = ["-s", serial, "logcat", "-d", "-v", "threadtime", "-t", str(lines)]
        if pid is not None:
            args.append(f"--pid={pid}")
        return self._exec(args, max_bytes=self.limits.logcat_max_bytes)


def parse_devices(text: str) -> list[Device]:
    """Parse ``adb devices -l``."""
    devices: list[Device] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("*", "List of devices")):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        props = dict(p.split(":", 1) for p in parts[2:] if ":" in p)
        devices.append(
            Device(
                serial=parts[0],
                state=parts[1],
                model=props.get("model"),
                product=props.get("product"),
                transport_id=props.get("transport_id"),
            )
        )
    return devices


def parse_am_start(component: str, text: str) -> LaunchOutcome:
    """Parse ``am start -W`` output. am exits 0 even on errors, so read the text."""
    error = re.search(r"^Error(?: type \d+)?:?\s*(.*)$", text, flags=re.MULTILINE)
    if error and "Status: ok" not in text:
        messages = [m for m in re.findall(r"^Error:\s*(.*)$", text, flags=re.MULTILINE) if m]
        raise CommandFailed(f"am start failed: {messages[-1] if messages else error.group(0)}")
    status = re.search(r"^Status:\s*(\S+)", text, flags=re.MULTILINE)
    state = re.search(r"^LaunchState:\s*(\S+)", text, flags=re.MULTILINE)
    total = re.search(r"^TotalTime:\s*(\d+)", text, flags=re.MULTILINE)
    return LaunchOutcome(
        component=component,
        status=status.group(1) if status else "unknown",
        launch_state=state.group(1) if state else None,
        total_time_ms=int(total.group(1)) if total else None,
        dry_run=False,
    )
