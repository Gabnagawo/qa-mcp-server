"""Security test matrix.

Two layers are attacked:

1. **Tool layer** - what a prompt-injected agent can actually send: every case is
   a ``tools/call`` through a real MCP client session. Each must come back as an
   error, be audited with outcome ``rejected``, and must not have caused any
   state-changing adb command on the device.
2. **Command layer** - raw adb argv lists, as if a bug upstream produced them.
   Each must be refused by the allowlist before reaching a transport.

``pytest -m security`` runs only this matrix.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any

import pytest
from mcp import ClientSession

from qa_mcp_server.audit import AuditLogger
from qa_mcp_server.errors import PolicyViolation
from qa_mcp_server.fake import FakeAdbTransport
from qa_mcp_server.policy import validate_adb_args

from .conftest import APP, mutating_calls

SEC = pytest.mark.security

# -- tool layer --------------------------------------------------------------------

COMMAND_INJECTION: list[tuple[str, dict[str, Any]]] = [
    ("input_text", {"text": "hello; reboot"}),
    ("input_text", {"text": "a && rm -rf /sdcard"}),
    ("input_text", {"text": "$(reboot)"}),
    ("input_text", {"text": "`id`"}),
    ("input_text", {"text": "a | sh"}),
    ("input_text", {"text": "a > /sdcard/owned"}),
    ("input_text", {"text": "line1\nreboot"}),
    ("input_text", {"text": "it's"}),
    ("input_text", {"text": 'say "hi"'}),
    ("input_text", {"text": "back\\slash"}),
    ("input_text", {"text": "${IFS}reboot"}),
    ("input_text", {"text": "glob*"}),
    ("input_text", {"text": "-e foo"}),
    ("launch_app", {"package": "org.a.b; reboot"}),
    ("launch_app", {"package": "org.a.b && id"}),
    ("launch_app", {"package": "$(id).evil"}),
    ("launch_app", {"package": "org.a.b -S"}),
    ("launch_app", {"package": "../../system"}),
    ("launch_app", {"package": APP, "activity": ".Main; reboot"}),
    ("launch_app", {"package": APP, "activity": ".Main$Inner"}),
    ("launch_app", {"package": APP, "activity": ".Main -a android.intent.action.CALL"}),
    ("launch_app", {"package": APP, "activity": "../Main"}),
    ("list_devices_serial", {"serial": "emulator-5554; reboot"}),
    ("dump_ui", {"serial": "-H evil.example.com"}),
    ("dump_ui", {"serial": "$(id)"}),
    ("screenshot", {"serial": "emulator 5554"}),
    ("tap", {"x": 10, "y": 10, "serial": "emu\nlator"}),
    ("logcat", {"tag": "Tag; reboot"}),
    ("logcat", {"tag": "*:S"}),
    ("logcat", {"since": "11-12 10:00:00; reboot"}),
    ("logcat", {"since": "$(date)"}),
    ("logcat", {"package": "org.a; reboot"}),
    ("press_key", {"key": "BACK; reboot"}),
    ("press_key", {"key": "KEYCODE_BACK KEYCODE_HOME"}),
    ("press_key", {"key": "26"}),
    ("run_test_suite", {"name": "smoke; rm -rf /"}),
    ("run_test_suite", {"name": "../smoke"}),
    ("run_test_suite", {"name": "smoke --collect-only"}),
    ("tap", {"x": "10; reboot", "y": 10}),
]

PATH_TRAVERSAL: list[str] = [
    "../../../etc/passwd",
    "..\\..\\Windows\\win.ini",
    "/etc/passwd",
    "sample-notes-1.0.apk/../../outside.apk",
    "../outside.apk",
    "file:///etc/passwd",
    "%2e%2e/outside.apk",
    "sample-notes-1.0.apk\x00.txt",
    "not-a-zip.apk",
    "wrong-extension.zip",
    "evil;id.apk",
    "evil name.apk",
]

POLICY: list[tuple[str, dict[str, Any]]] = [
    ("shell", {"command": "reboot"}),
    ("adb", {"args": ["root"]}),
    ("press_key", {"key": "POWER"}),
    ("press_key", {"key": "KEYCODE_SLEEP"}),
    ("run_test_suite", {"name": "not-configured"}),
    ("tap", {"x": 10}),
    ("tap", {"x": 10, "y": 10, "text": "Save"}),
    ("tap", {"text": "Save", "content_desc": "Save"}),
]

LIMITS: list[tuple[str, dict[str, Any]]] = [
    ("input_text", {"text": "a" * 501}),
    ("logcat", {"max_lines": 100_000}),
    ("tap", {"x": -1, "y": 5}),
    ("tap", {"x": 5, "y": 99_999}),
    ("tap", {"text": "x" * 201}),
    ("tap", {"text": "Save\x00"}),
    ("tap", {"text": "Save", "index": 1000}),
    ("install_apk", {"apk_path": "a" * 5000}),
]


@pytest.fixture
def traversal_layout(apk_dir: Path) -> Path:
    outside = apk_dir.parent / "outside.apk"
    outside.write_bytes(b"PK\x03\x04 outside the sandbox")
    (apk_dir / "not-a-zip.apk").write_text("plain text pretending to be an APK")
    (apk_dir / "wrong-extension.zip").write_bytes(b"PK\x03\x04 zip")
    for name in ("evil;id.apk", "evil name.apk"):
        try:
            (apk_dir / name).write_bytes(b"PK\x03\x04 bad name")
        except OSError:  # pragma: no cover - filesystem without these characters
            pass
    return outside


async def _assert_rejected(
    client: ClientSession,
    audit: AuditLogger,
    fake: FakeAdbTransport,
    tool: str,
    args: dict[str, Any],
) -> None:
    result = await client.call_tool(tool, args)
    assert result.isError, f"{tool}({args!r}) was accepted"
    record = audit.read()[-1]
    assert record["tool"] == tool
    assert record["outcome"] == "rejected", record
    assert mutating_calls(fake) == [], "a state-changing command reached the device"


def _ids(cases: list[tuple[str, dict[str, Any]]]) -> list[str]:
    return [f"{tool}:{next(iter(args.values()))!r}"[:60] for tool, args in cases]


@SEC
@pytest.mark.anyio
@pytest.mark.parametrize(("tool", "args"), COMMAND_INJECTION, ids=_ids(COMMAND_INJECTION))
async def test_command_injection_rejected(
    client: ClientSession,
    audit: AuditLogger,
    fake: FakeAdbTransport,
    tool: str,
    args: dict[str, Any],
) -> None:
    # list_devices takes no serial; route serial injection through a tool that does.
    tool = "logcat" if tool == "list_devices_serial" else tool
    await _assert_rejected(client, audit, fake, tool, args)


@SEC
@pytest.mark.anyio
@pytest.mark.parametrize("apk_path", PATH_TRAVERSAL)
async def test_path_traversal_rejected(
    client: ClientSession,
    audit: AuditLogger,
    fake: FakeAdbTransport,
    traversal_layout: Path,
    apk_path: str,
) -> None:
    await _assert_rejected(client, audit, fake, "install_apk", {"apk_path": apk_path})


@SEC
@pytest.mark.anyio
async def test_absolute_path_outside_sandbox_rejected(
    client: ClientSession, audit: AuditLogger, fake: FakeAdbTransport, traversal_layout: Path
) -> None:
    await _assert_rejected(client, audit, fake, "install_apk", {"apk_path": str(traversal_layout)})


@SEC
@pytest.mark.anyio
async def test_symlink_escaping_sandbox_rejected(
    client: ClientSession,
    audit: AuditLogger,
    fake: FakeAdbTransport,
    apk_dir: Path,
    traversal_layout: Path,
) -> None:
    link = apk_dir / "link.apk"
    try:
        link.symlink_to(traversal_layout)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not permitted on this system")
    await _assert_rejected(client, audit, fake, "install_apk", {"apk_path": "link.apk"})


@SEC
@pytest.mark.anyio
@pytest.mark.parametrize(("tool", "args"), POLICY, ids=_ids(POLICY))
async def test_policy_violations_rejected(
    client: ClientSession,
    audit: AuditLogger,
    fake: FakeAdbTransport,
    tool: str,
    args: dict[str, Any],
) -> None:
    await _assert_rejected(client, audit, fake, tool, args)


@SEC
@pytest.mark.anyio
@pytest.mark.parametrize(("tool", "args"), LIMITS, ids=_ids(LIMITS))
async def test_limits_enforced(
    client: ClientSession,
    audit: AuditLogger,
    fake: FakeAdbTransport,
    tool: str,
    args: dict[str, Any],
) -> None:
    await _assert_rejected(client, audit, fake, tool, args)


# -- command layer -----------------------------------------------------------------

S = ["-s", "emulator-5554"]

RAW_ARGV: list[list[str]] = [
    ["shell", "reboot"],
    ["reboot"],
    ["root"],
    ["tcpip", "5555"],
    ["connect", "203.0.113.5:5555"],
    ["-H", "evil.example.com", "devices", "-l"],
    ["-P", "5038", "devices", "-l"],
    ["devices"],
    ["devices", "-l", "extra"],
    [*S, "shell"],
    [*S, "shell", "reboot"],
    [*S, "shell", "rm", "-rf", "/sdcard"],
    [*S, "shell", "sh", "-c", "id"],
    [*S, "exec-out", "sh"],
    [*S, "exec-out", "run-as", "org.a.b", "cat", "databases/app.db"],
    [*S, "uninstall", "org.a.b"],
    [*S, "pull", "/data/data/org.a.b", "/tmp/loot"],
    [*S, "push", "/tmp/payload", "/data/local/tmp/payload"],
    [*S, "forward", "tcp:8000", "tcp:8000"],
    [*S, "emu", "kill"],
    [*S, "shell", "input", "text", "a;reboot"],
    [*S, "shell", "input", "text", "$(reboot)"],
    [*S, "shell", "input", "text", "a&&id"],
    [*S, "shell", "input", "text", "a|id"],
    [*S, "shell", "input", "text", "a\nreboot"],
    [*S, "shell", "input", "keyevent", "KEYCODE_POWER"],
    [*S, "shell", "input", "keyevent", "26"],
    [*S, "shell", "input", "tap", "-1", "5"],
    [*S, "shell", "input", "tap", "99999", "5"],
    [*S, "shell", "input", "swipe", "0", "0", "10", "10"],
    [*S, "shell", "am", "start", "-W", "-n", "org.a/.M;reboot"],
    [*S, "shell", "am", "start", "-W", "-n", "org.a/.M", "--user", "0"],
    [*S, "shell", "am", "start", "-a", "android.intent.action.CALL", "-d", "tel:123"],
    [*S, "shell", "am", "broadcast", "-a", "android.intent.action.MASTER_CLEAR"],
    [*S, "shell", "pm", "clear", "org.a.b"],
    [*S, "shell", "pm", "grant", "org.a.b", "android.permission.READ_SMS"],
    [*S, "shell", "settings", "put", "global", "adb_enabled", "0"],
    [*S, "install", "-r", "-g", "/x/app.apk"],
    [*S, "install", "-r", "relative/app.apk"],
    [*S, "install", "-r", "/x/evil;id.apk"],
    [*S, "install", "-r", "/x/payload.sh"],
    [*S, "install", "-r", "/x/app.apk\x00.sh"],
    [*S, "logcat", "-c"],
    [*S, "logcat", "-f", "/sdcard/out.txt"],
    [*S, "logcat", "-d", "-v", "threadtime", "-t", "10", "--pid=1;id"],
    [*S, "logcat", "-d", "-v", "threadtime", "-t", "0"],
    ["-s", "a;b", "devices", "-l"],
    ["-s", "-H", "shell", "id"],
    ["-s", "emulator-5554"],
]


@SEC
@pytest.mark.parametrize("argv", RAW_ARGV, ids=[" ".join(a)[:50] for a in RAW_ARGV])
def test_raw_adb_argv_rejected_by_allowlist(argv: list[str]) -> None:
    with pytest.raises(PolicyViolation):
        validate_adb_args(argv)


# -- package allowlist -------------------------------------------------------------


@SEC
@pytest.mark.anyio
async def test_package_outside_allowed_packages_rejected(tmp_path: Path, apk_dir: Path) -> None:
    from mcp.shared.memory import create_connected_server_and_client_session

    from qa_mcp_server.config import Settings
    from qa_mcp_server.server import build_server, build_service

    settings = Settings(
        backend="fake",
        apk_dir=apk_dir,
        artifacts_dir=tmp_path / "a",
        allowed_packages=("org.qamcp.*",),
    )
    fake = FakeAdbTransport.from_file()
    server = build_server(settings, service=build_service(settings, transport=fake))
    async with create_connected_server_and_client_session(server) as session:
        await _assert_rejected(
            session, server.audit, fake, "launch_app", {"package": "com.android.settings"}
        )
        await _assert_rejected(
            session, server.audit, fake, "logcat", {"package": "com.android.settings"}
        )
