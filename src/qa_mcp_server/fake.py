"""A scripted fake Android device that speaks the adb argv protocol.

:class:`FakeAdbTransport` implements :class:`~qa_mcp_server.transport.Transport`
and answers the exact argv lists that :class:`~qa_mcp_server.adb.AdbClient`
produces, with byte-compatible output (``adb devices -l``, ``am start -W``,
``uiautomator dump``, threadtime logcat, PNG screencap). Its state is scripted
by a JSON scenario: devices, installed packages, UI screens as fixture XML,
tap transitions between screens, logcat lines and a screenshot.

Because the fake sits *below* the policy check, every test and demo run against
it also exercises the real allowlist, argv building and output parsing.
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from xml.sax.saxutils import escape

from pydantic import BaseModel, ConfigDict, Field

from qa_mcp_server.transport import RunResult
from qa_mcp_server.uitree import UiNode, iter_nodes, parse_hierarchy

_PLACEHOLDER_RE = re.compile(r"\{\{typed:([^|}]+)(?:\|([^}]*))?\}\}")
_LOGCAT_PID_RE = re.compile(r"^\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}\s+(\d+)\s")


class FakeDeviceSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    serial: str
    state: str = "device"
    model: str = "Pixel_7_API_34"
    product: str = "sdk_gphone64_x86_64"
    transport_id: str = "1"


class FakePackage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    installed: bool = True
    launcher_activity: str | None = None
    activities: list[str] = Field(default_factory=list)
    start_screen: str | None = None
    pid: int = 4242
    launch_time_ms: int = 412
    launch_logcat: list[str] = Field(default_factory=list)


class FakeTransition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    screen: str
    text: str | None = None
    resource_id: str | None = None
    content_desc: str | None = None
    to: str | None = None
    back: bool = False
    launch: str | None = None
    crash: str | None = Field(default=None, description="Package that crashes on this tap.")
    logcat: list[str] = Field(default_factory=list)

    def matches(self, node: UiNode) -> bool:
        checks = [
            (self.text, node.text),
            (self.resource_id, node.resource_id),
            (self.content_desc, node.content_desc),
        ]
        given = [(want, have) for want, have in checks if want is not None]
        return bool(given) and all(want == have for want, have in given)


class FakeScenario(BaseModel):
    model_config = ConfigDict(extra="forbid")
    devices: list[FakeDeviceSpec]
    packages: dict[str, FakePackage] = Field(default_factory=dict)
    apks: dict[str, str] = Field(default_factory=dict, description="APK file name -> package")
    home_screen: str
    screens: dict[str, str] = Field(description="Screen name -> XML file (relative to scenario)")
    transitions: list[FakeTransition] = Field(default_factory=list)
    logcat: str | None = Field(default=None, description="Logcat fixture file")
    screenshot: str = Field(description="PNG fixture file")


@dataclass
class _DeviceState:
    installed: set[str]
    screen: str
    back_stack: list[str] = field(default_factory=list)
    running: dict[str, int] = field(default_factory=dict)
    typed: dict[str, str] = field(default_factory=dict)
    focused: str | None = None
    logcat: list[str] = field(default_factory=list)


def bundled_scenario_path() -> Path:
    return Path(str(resources.files("qa_mcp_server") / "fixtures" / "notes" / "scenario.json"))


class FakeAdbTransport:
    """Scripted device. Thread-safe; records every argv it receives in ``calls``."""

    def __init__(self, scenario: FakeScenario, base_dir: Path) -> None:
        self.scenario = scenario
        self.base_dir = base_dir
        self.screens = {
            name: (base_dir / file).read_text(encoding="utf-8")
            for name, file in scenario.screens.items()
        }
        self.screenshot = (base_dir / scenario.screenshot).read_bytes()
        base_logcat = (
            (base_dir / scenario.logcat).read_text(encoding="utf-8").splitlines()
            if scenario.logcat
            else []
        )
        self.state = _DeviceState(
            installed={name for name, pkg in scenario.packages.items() if pkg.installed},
            screen=scenario.home_screen,
            logcat=[line for line in base_logcat if line.strip()],
        )
        self.calls: list[tuple[str, ...]] = []
        self._lock = threading.Lock()

    @classmethod
    def from_file(cls, path: Path | None = None) -> FakeAdbTransport:
        path = path or bundled_scenario_path()
        scenario = FakeScenario.model_validate(json.loads(path.read_text(encoding="utf-8")))
        return cls(scenario, path.parent)

    # -- Transport protocol ----------------------------------------------------------

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout_s: float,
        max_bytes: int,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> RunResult:
        args = tuple(argv)
        with self._lock:
            self.calls.append(args)
            rc, out, err = self._dispatch(list(args[1:]))
        truncated = len(out) > max_bytes
        return RunResult(args, rc, out[:max_bytes], err, 0.0, truncated=truncated)

    # -- dispatch ----------------------------------------------------------------------

    def _dispatch(self, args: list[str]) -> tuple[int, bytes, bytes]:
        if args == ["devices", "-l"]:
            return 0, self._devices_output().encode(), b""
        if len(args) < 3 or args[0] != "-s":
            return 1, b"", b"adb: usage error"
        serial, cmd = args[1], args[2:]
        device = next((d for d in self.scenario.devices if d.serial == serial), None)
        if device is None:
            return 1, b"", f"adb: device '{serial}' not found".encode()
        if device.state != "device":
            return 1, b"", f"adb: device {device.state}".encode()
        return self._device_command(cmd)

    def _device_command(self, cmd: list[str]) -> tuple[int, bytes, bytes]:
        head = tuple(cmd[:3])
        if cmd[:2] == ["install", "-r"]:
            return self._install(cmd[2])
        if cmd[:3] == ["shell", "pm", "path"]:
            pkg = cmd[3]
            if pkg in self.state.installed:
                return 0, f"package:/data/app/{pkg}-1/base.apk\n".encode(), b""
            return 1, b"", b""
        if cmd[:4] == ["shell", "cmd", "package", "resolve-activity"]:
            return self._resolve(cmd[-1])
        if head == ("shell", "am", "start"):
            return self._am_start(cmd[-1])
        if cmd[:2] == ["shell", "pidof"]:
            pid = self.state.running.get(cmd[2])
            return (0, f"{pid}\n".encode(), b"") if pid else (1, b"", b"")
        if cmd == ["exec-out", "screencap", "-p"]:
            return 0, self.screenshot, b""
        if cmd == ["exec-out", "uiautomator", "dump", "/dev/tty"]:
            xml = self.render(self.state.screen)
            return 0, (xml + "UI hierchary dumped to: /dev/tty\n").encode(), b""
        if head == ("shell", "input", "tap"):
            self._tap(int(cmd[3]), int(cmd[4]))
            return 0, b"", b""
        if head == ("shell", "input", "text"):
            if self.state.focused:
                text = cmd[3].replace("%s", " ")
                self.state.typed[self.state.focused] = (
                    self.state.typed.get(self.state.focused, "") + text
                )
            return 0, b"", b""
        if head == ("shell", "input", "keyevent"):
            self._key(cmd[3])
            return 0, b"", b""
        if cmd[:1] == ["logcat"]:
            return 0, self._logcat(cmd).encode(), b""
        return 1, b"", f"fake device: unsupported command {cmd!r}".encode()

    # -- behaviours ----------------------------------------------------------------------

    def _devices_output(self) -> str:
        lines = ["List of devices attached"]
        for d in self.scenario.devices:
            lines.append(
                f"{d.serial:<22} {d.state} product:{d.product} model:{d.model} "
                f"device:emu64x transport_id:{d.transport_id}"
            )
        return "\n".join(lines) + "\n\n"

    def _install(self, path: str) -> tuple[int, bytes, bytes]:
        name = Path(path.replace("\\", "/")).name
        package = self.scenario.apks.get(name)
        if package is None:
            return (
                1,
                b"Performing Streamed Install\n",
                b"adb: failed to install: Failure [INSTALL_PARSE_FAILED_NOT_APK]",
            )
        self.state.installed.add(package)
        return 0, b"Performing Streamed Install\nSuccess\n", b""

    def _resolve(self, package: str) -> tuple[int, bytes, bytes]:
        spec = self.scenario.packages.get(package)
        if package not in self.state.installed or spec is None or not spec.launcher_activity:
            return 0, b"No activity found\n", b""
        header = "priority=0 preferredOrder=0 match=0x108000 specificIndex=-1 isDefault=false"
        return 0, f"{header}\n{package}/{spec.launcher_activity}\n".encode(), b""

    def _am_start(self, component: str) -> tuple[int, bytes, bytes]:
        package, _, activity = component.partition("/")
        spec = self.scenario.packages.get(package)
        full = package + activity if activity.startswith(".") else activity
        known = (
            {package + a if a.startswith(".") else a for a in spec.activities} if spec else set()
        )
        if package not in self.state.installed or spec is None or full not in known:
            out = (
                f"Starting: Intent {{ cmp={component} }}\n"
                "Error type 3\n"
                f"Error: Activity class {{{package}/{full}}} does not exist.\n"
            )
            return 0, out.encode(), b""
        cold = package not in self.state.running
        self.state.running[package] = spec.pid
        if spec.start_screen:
            self._navigate(spec.start_screen, push=False, reset=True)
        launch_ms = spec.launch_time_ms if cold else max(spec.launch_time_ms // 5, 1)
        for line in spec.launch_logcat if cold else []:
            self._log(line, spec.pid)
        out = (
            f"Starting: Intent {{ cmp={component} }}\n"
            "Status: ok\n"
            f"LaunchState: {'COLD' if cold else 'HOT'}\n"
            f"Activity: {component}\n"
            f"TotalTime: {launch_ms}\n"
            f"WaitTime: {launch_ms + 3}\n"
            "Complete\n"
        )
        return 0, out.encode(), b""

    def _log(self, line: str, pid: int | None) -> None:
        self.state.logcat.append(line.replace("{pid}", str(pid or 0)))

    def _navigate(self, screen: str, *, push: bool = True, reset: bool = False) -> None:
        if reset:
            self.state.back_stack.clear()
        elif push:
            self.state.back_stack.append(self.state.screen)
        self.state.screen = screen
        self.state.focused = None

    def _hit(self, x: int, y: int) -> UiNode | None:
        """Deepest node containing (x, y); clickable or editable nodes win."""
        best: UiNode | None = None
        for node in iter_nodes(parse_hierarchy(self.render(self.state.screen))):
            if node.bounds is None:
                continue
            x1, y1, x2, y2 = node.bounds
            if (
                x1 <= x < x2
                and y1 <= y < y2
                and ("clickable" in node.flags or node.cls == "EditText")
            ):
                best = node
        return best

    def _tap(self, x: int, y: int) -> None:
        node = self._hit(x, y)
        if node is None:
            return
        if node.cls == "EditText" and node.resource_id:
            self.state.focused = node.resource_id
        for rule in self.scenario.transitions:
            if rule.screen != self.state.screen or not rule.matches(node):
                continue
            pid = None
            if rule.crash:
                pid = self.state.running.pop(rule.crash, None)
            if rule.launch:
                spec = self.scenario.packages[rule.launch]
                self.state.running.setdefault(rule.launch, spec.pid)
                pid = spec.pid
            for line in rule.logcat:
                self._log(line, pid or self._current_pid())
            if rule.back:
                self._key("KEYCODE_BACK")
            elif rule.to:
                self._navigate(rule.to, reset=bool(rule.crash or rule.launch))
            return

    def _current_pid(self) -> int | None:
        return next(iter(self.state.running.values()), None)

    def _key(self, keycode: str) -> None:
        if keycode == "KEYCODE_HOME":
            self._navigate(self.scenario.home_screen, reset=True)
        elif keycode == "KEYCODE_BACK":
            if self.state.back_stack:
                self.state.screen = self.state.back_stack.pop()
            else:
                self.state.screen = self.scenario.home_screen
            self.state.focused = None
        elif keycode == "KEYCODE_DEL" and self.state.focused:
            current = self.state.typed.get(self.state.focused, "")
            self.state.typed[self.state.focused] = current[:-1]

    def _logcat(self, cmd: list[str]) -> str:
        count = int(cmd[cmd.index("-t") + 1])
        pid_arg = next((a for a in cmd if a.startswith("--pid=")), None)
        lines = self.state.logcat
        if pid_arg:
            pid = pid_arg.removeprefix("--pid=")
            lines = [ln for ln in lines if (m := _LOGCAT_PID_RE.match(ln)) and m.group(1) == pid]
        selected = lines[-count:]
        header = "--------- beginning of main\n"
        return header + "".join(line + "\n" for line in selected)

    def render(self, screen: str) -> str:
        """Screen XML with ``{{typed:<resource-id>|default}}`` placeholders filled in."""

        def fill(match: re.Match[str]) -> str:
            typed = self.state.typed.get(match.group(1))
            return escape(typed, {'"': "&quot;"}) if typed else (match.group(2) or "")

        xml = _PLACEHOLDER_RE.sub(fill, self.screens[screen])
        if self.state.focused and screen == self.state.screen:
            # uiautomator reports input focus as focused="true" on the node.
            pattern = re.compile(
                r'(<node [^>]*resource-id="' + re.escape(self.state.focused) + r'"[^>]*?)'
                r'focused="false"'
            )
            xml = pattern.sub(r'\g<1>focused="true"', xml, count=1)
        return xml
