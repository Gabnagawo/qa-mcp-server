from __future__ import annotations

import sys
from pathlib import Path

import pytest

from qa_mcp_server.config import Settings, TestSuiteConfig
from qa_mcp_server.errors import CommandFailed, DeviceError, PolicyViolation
from qa_mcp_server.fake import FakeAdbTransport, FakeDeviceSpec
from qa_mcp_server.server import build_service
from qa_mcp_server.service import QAService
from qa_mcp_server.validation import (
    DumpUiRequest,
    InstallRequest,
    LaunchRequest,
    LogcatRequest,
    RunSuiteRequest,
    ScreenshotRequest,
    Selector,
    TapRequest,
)

from .conftest import APP


def _launch(service: QAService) -> None:
    service.install_apk(InstallRequest(apk_path="sample-notes-1.0.apk"))
    service.launch_app(LaunchRequest(package=APP))


def test_resolve_serial_variants(service: QAService, fake: FakeAdbTransport) -> None:
    assert service.resolve_serial(None) == "emulator-5554"
    with pytest.raises(DeviceError, match="not attached or not online"):
        service.resolve_serial("R5CT0000FAKE")
    with pytest.raises(PolicyViolation):
        service.resolve_serial("bad serial")
    fake.scenario.devices.append(FakeDeviceSpec(serial="emulator-5556"))
    with pytest.raises(DeviceError, match="several devices"):
        service.resolve_serial(None)
    fake.scenario.devices.clear()
    with pytest.raises(DeviceError, match="no online device"):
        service.resolve_serial(None)


def test_launch_requires_installed_package(service: QAService) -> None:
    with pytest.raises(CommandFailed, match="not installed"):
        service.launch_app(LaunchRequest(package=APP))


def test_launch_with_explicit_activity(service: QAService) -> None:
    service.install_apk(InstallRequest(apk_path="sample-notes-1.0.apk"))
    result = service.launch_app(LaunchRequest(package=APP, activity=".MainActivity"))
    assert result.launch_state == "COLD"
    assert result.total_time_ms == 412


def test_launch_without_launcher_activity(service: QAService, fake: FakeAdbTransport) -> None:
    fake.scenario.packages["com.android.settings"].launcher_activity = None
    with pytest.raises(CommandFailed, match="no launcher activity"):
        service.launch_app(LaunchRequest(package="com.android.settings"))


def test_logcat_filters(service: QAService) -> None:
    _launch(service)
    everything = service.logcat(LogcatRequest())
    assert everything.returned == everything.scanned == 10
    errors = service.logcat(LogcatRequest(level="E"))
    assert [e.tag for e in errors.entries] == ["netmgr"]
    by_tag = service.logcat(LogcatRequest(tag="NotesRepository"))
    assert by_tag.returned == 1
    by_pkg = service.logcat(LogcatRequest(package=APP))
    assert {e.pid for e in by_pkg.entries} == {4242}
    since = service.logcat(LogcatRequest(since="2026-11-12 10:15:03"))
    assert all(e.ts >= "11-12 10:15:03" for e in since.entries)
    assert since.returned == 4
    capped = service.logcat(LogcatRequest(max_lines=3))
    assert capped.returned == 3
    assert capped.truncated is True


def test_logcat_package_not_running(service: QAService) -> None:
    result = service.logcat(LogcatRequest(package=APP))
    assert result.returned == 0
    assert result.note is not None
    assert "not running" in result.note


def test_screenshot_save(service: QAService, settings: Settings) -> None:
    png, saved = service.screenshot(ScreenshotRequest(save=True))
    assert saved is not None
    path = Path(saved.path)
    assert path.read_bytes() == png
    assert path.parent == settings.artifacts_dir / "screenshots"


def test_tap_selector_errors(service: QAService) -> None:
    with pytest.raises(CommandFailed, match="no element"):
        service.tap(TapRequest(selector=Selector(text="Does not exist")))
    with pytest.raises(CommandFailed, match="index out of range"):
        service.tap(TapRequest(selector=Selector(text="Settings", index=3)))


def test_dump_ui_full(service: QAService) -> None:
    tree = service.dump_ui(DumpUiRequest(compact=False))
    assert tree["node_count"] == tree["total_nodes"]


def _suite_service(tmp_path: Path, apk_dir: Path, **overrides: object) -> QAService:
    script = tmp_path / "suite.py"
    script.write_text(
        "import os, sys\n"
        "print('serial', os.environ.get('ANDROID_SERIAL'))\n"
        "open('report.xml', 'w').write('<testsuites><testsuite tests=\"3\" failures=\"1\" "
        'errors="0" skipped="1"/></testsuites>\')\n'
        "sys.exit(1)\n"
    )
    suite = TestSuiteConfig(
        argv=(sys.executable, str(script)), cwd=tmp_path, junit_xml=Path("report.xml")
    )
    settings = Settings(
        backend="fake",
        apk_dir=apk_dir,
        artifacts_dir=tmp_path / "out",
        test_suites={"smoke": suite},
        **overrides,  # type: ignore[arg-type]
    )
    return build_service(settings, transport=FakeAdbTransport.from_file())


def test_run_test_suite_reports_exit_and_junit(tmp_path: Path, apk_dir: Path) -> None:
    service = _suite_service(tmp_path, apk_dir)
    result = service.run_test_suite(RunSuiteRequest(name="smoke", serial="emulator-5554"))
    assert result.exit_code == 1
    assert result.passed is False
    assert "serial emulator-5554" in result.output_tail
    assert result.junit == {"tests": 3, "failures": 1, "errors": 0, "skipped": 1}


def test_run_test_suite_dry_run(tmp_path: Path, apk_dir: Path) -> None:
    service = _suite_service(tmp_path, apk_dir, dry_run=True)
    result = service.run_test_suite(RunSuiteRequest(name="smoke"))
    assert result.dry_run is True
    assert result.exit_code is None
    assert not (tmp_path / "report.xml").exists()


def test_junit_outside_cwd_or_invalid_is_ignored(tmp_path: Path, apk_dir: Path) -> None:
    from qa_mcp_server.service import _read_junit

    (tmp_path / "bad.xml").write_text("<not-closed")
    assert (
        _read_junit(TestSuiteConfig(argv=("x",), cwd=tmp_path, junit_xml=Path("bad.xml"))) is None
    )
    assert (
        _read_junit(TestSuiteConfig(argv=("x",), cwd=tmp_path, junit_xml=Path("../escape.xml")))
        is None
    )
    (tmp_path / "one.xml").write_text('<testsuite tests="2" failures="0"/>')
    one = _read_junit(TestSuiteConfig(argv=("x",), cwd=tmp_path, junit_xml=Path("one.xml")))
    assert one == {"tests": 2, "failures": 0, "errors": 0, "skipped": 0}
    assert _read_junit(TestSuiteConfig(argv=("x",))) is None
