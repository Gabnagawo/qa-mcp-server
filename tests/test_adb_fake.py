from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from qa_mcp_server.adb import AdbClient, parse_am_start, parse_devices
from qa_mcp_server.audit import capture_commands
from qa_mcp_server.errors import CommandFailed, OutputTooLarge, PolicyViolation
from qa_mcp_server.fake import FakeAdbTransport, bundled_scenario_path
from qa_mcp_server.transport import RunResult

from .conftest import APP

SERIAL = "emulator-5554"


class StaticTransport:
    """Returns one canned result for every call."""

    def __init__(self, stdout: bytes, returncode: int = 0, truncated: bool = False) -> None:
        self.result = (stdout, returncode, truncated)
        self.calls: list[tuple[str, ...]] = []

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout_s: float,
        max_bytes: int,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> RunResult:
        self.calls.append(tuple(argv))
        out, rc, truncated = self.result
        return RunResult(tuple(argv), rc, out, b"boom" if rc else b"", 0.0, truncated=truncated)


def test_parse_devices_handles_daemon_noise() -> None:
    text = (
        "* daemon not running; starting now at tcp:5037\n"
        "* daemon started successfully\n"
        "List of devices attached\n"
        "emulator-5554 device product:sdk model:Pixel_7 device:emu transport_id:1\n"
        "R58M unauthorized usb:1-1 transport_id:3\n"
        "weird\n"
    )
    devices = parse_devices(text)
    assert [(d.serial, d.state) for d in devices] == [
        ("emulator-5554", "device"),
        ("R58M", "unauthorized"),
    ]
    assert devices[0].model == "Pixel_7"
    assert devices[1].model is None


def test_parse_am_start_ok_and_error() -> None:
    ok = parse_am_start("a.b/.M", "Status: ok\nLaunchState: COLD\nTotalTime: 412\n")
    assert (ok.status, ok.launch_state, ok.total_time_ms) == ("ok", "COLD", 412)
    with pytest.raises(CommandFailed, match="does not exist"):
        parse_am_start(
            "a.b/.X", "Error type 3\nError: Activity class {a.b/a.b.X} does not exist.\n"
        )
    with pytest.raises(CommandFailed, match="Error type 1"):
        parse_am_start("a.b/.X", "Error type 1\n")
    unknown = parse_am_start("a.b/.M", "Starting: Intent\n")
    assert unknown.status == "unknown"
    assert unknown.total_time_ms is None


def test_policy_runs_before_transport() -> None:
    transport = StaticTransport(b"")
    adb = AdbClient(transport)
    with pytest.raises(PolicyViolation):
        adb.tap(SERIAL, 10, 99_999)
    assert transport.calls == []


def test_dry_run_skips_mutating_but_runs_read_only() -> None:
    fake = FakeAdbTransport.from_file()
    adb = AdbClient(fake, dry_run=True)
    with capture_commands() as commands:
        assert adb.tap(SERIAL, 5, 5).dry_run
        assert adb.start_activity(SERIAL, f"{APP}/.MainActivity").dry_run
        assert adb.install(SERIAL, "/x/sample-notes-1.0.apk").dry_run
        assert adb.devices()
    assert [c.executed for c in commands] == [False, False, False, True]
    assert fake.calls == [("adb", "devices", "-l")]


def test_nonzero_exit_raises_with_stderr() -> None:
    adb = AdbClient(StaticTransport(b"", returncode=1))
    with pytest.raises(CommandFailed, match="boom"):
        adb.devices()


def test_install_failure_text() -> None:
    adb = AdbClient(StaticTransport(b"Failure [INSTALL_FAILED_OLDER_SDK]"))
    with pytest.raises(CommandFailed, match="OLDER_SDK"):
        adb.install(SERIAL, "/x/app.apk")


def test_screencap_rejects_non_png_and_oversize() -> None:
    with pytest.raises(CommandFailed, match="PNG"):
        AdbClient(StaticTransport(b"not a png")).screencap(SERIAL)
    with pytest.raises(OutputTooLarge):
        AdbClient(StaticTransport(b"\x89PNG", truncated=True)).screencap(SERIAL)


def test_ui_dump_oversize() -> None:
    with pytest.raises(OutputTooLarge):
        AdbClient(StaticTransport(b"<hierarchy>", truncated=True)).ui_dump(SERIAL)


def test_pidof_and_launcher_parsing() -> None:
    adb = AdbClient(StaticTransport(b"garbage"))
    assert adb.pidof(SERIAL, "a.b") is None
    assert adb.resolve_launcher(SERIAL, "a.b") is None


# -- fake device behaviour ---------------------------------------------------------


def test_fake_device_errors() -> None:
    fake = FakeAdbTransport.from_file()
    adb = AdbClient(fake)
    with pytest.raises(CommandFailed, match="not found"):
        adb.tap("unknown-1", 1, 1)
    with pytest.raises(CommandFailed, match="offline"):
        adb.tap("R5CT0000FAKE", 1, 1)
    rc = fake.run(("adb", "bogus"), timeout_s=1, max_bytes=10)
    assert rc.returncode == 1
    unsupported = fake.run(("adb", "-s", SERIAL, "shell", "id"), timeout_s=1, max_bytes=100)
    assert b"unsupported" in unsupported.stderr


def test_fake_install_unknown_apk_fails() -> None:
    adb = AdbClient(FakeAdbTransport.from_file())
    with pytest.raises(CommandFailed, match="NOT_APK"):
        adb.install(SERIAL, "/x/other.apk")


def test_fake_launch_flow_cold_then_hot() -> None:
    fake = FakeAdbTransport.from_file()
    adb = AdbClient(fake)
    assert adb.resolve_launcher(SERIAL, APP) is None  # not installed yet
    with pytest.raises(CommandFailed, match="does not exist"):
        adb.start_activity(SERIAL, f"{APP}/.MainActivity")
    adb.install(SERIAL, "/x/sample-notes-1.0.apk")
    assert adb.is_installed(SERIAL, APP)
    component = adb.resolve_launcher(SERIAL, APP)
    assert component == f"{APP}/.MainActivity"
    assert adb.start_activity(SERIAL, component).launch_state == "COLD"
    hot = adb.start_activity(SERIAL, component)
    assert hot.launch_state == "HOT"
    assert hot.total_time_ms is not None
    assert hot.total_time_ms < 412
    assert adb.pidof(SERIAL, APP) == 4242


def test_fake_navigation_keys_and_text() -> None:
    fake = FakeAdbTransport.from_file()
    adb = AdbClient(fake)
    adb.install(SERIAL, "/x/sample-notes-1.0.apk")
    adb.tap(SERIAL, 150, 1800)  # launcher icon
    assert fake.state.screen == "notes_empty"
    adb.tap(SERIAL, 960, 2200)  # FAB
    assert fake.state.screen == "editor"
    adb.input_text(SERIAL, "ignored")  # nothing focused yet
    adb.tap(SERIAL, 500, 400)  # title field
    adb.input_text(SERIAL, "abc%sd")
    adb.keyevent(SERIAL, "KEYCODE_DEL")
    assert fake.state.typed[f"{APP}:id/title_input"] == "abc "
    adb.keyevent(SERIAL, "KEYCODE_BACK")
    assert fake.state.screen == "notes_empty"
    adb.keyevent(SERIAL, "KEYCODE_BACK")
    assert fake.state.screen == "launcher"
    adb.keyevent(SERIAL, "KEYCODE_BACK")  # empty back stack stays home
    adb.tap(SERIAL, 1, 1)  # empty space
    adb.keyevent(SERIAL, "KEYCODE_HOME")
    assert fake.state.screen == "launcher"


def test_fake_navigate_up_and_crash() -> None:
    fake = FakeAdbTransport.from_file()
    adb = AdbClient(fake)
    adb.install(SERIAL, "/x/sample-notes-1.0.apk")
    adb.start_activity(SERIAL, f"{APP}/.MainActivity")
    adb.tap(SERIAL, 960, 2200)
    adb.tap(SERIAL, 60, 200)  # Navigate up
    assert fake.state.screen == "notes_empty"
    adb.tap(SERIAL, 960, 2200)
    adb.tap(SERIAL, 114, 2250)  # Export as PDF -> crash
    assert fake.state.screen == "crash_dialog"
    assert adb.pidof(SERIAL, APP) is None
    log = adb.logcat(SERIAL, 50, pid=4242).text
    assert "FATAL EXCEPTION" in log
    assert "ActivityManager: Start proc" not in log  # pid filter keeps only app lines


def test_bundled_scenario_path_exists() -> None:
    assert bundled_scenario_path().is_file()
