from __future__ import annotations

import pytest

from qa_mcp_server.errors import PolicyViolation
from qa_mcp_server.policy import allowlist_summary, validate_adb_args

S = ["-s", "emulator-5554"]

ALLOWED = [
    (["devices", "-l"], "devices", False),
    ([*S, "install", "-r", "/home/u/apks/app-debug.apk"], "install", True),
    ([*S, "install", "-r", r"C:\Users\u\apks\app.apk"], "install", True),
    ([*S, "shell", "am", "start", "-W", "-n", "org.a.b/.Main"], "am_start", True),
    ([*S, "shell", "am", "start", "-W", "-n", "org.a.b/org.a.b.ui.Main"], "am_start", True),
    ([*S, "shell", "pm", "path", "org.a.b"], "pm_path", False),
    ([*S, "shell", "pidof", "org.a.b"], "pidof", False),
    ([*S, "exec-out", "screencap", "-p"], "screencap", False),
    ([*S, "exec-out", "uiautomator", "dump", "/dev/tty"], "ui_dump", False),
    ([*S, "shell", "input", "tap", "10", "20"], "tap", True),
    ([*S, "shell", "input", "text", "hello%sworld"], "input_text", True),
    ([*S, "shell", "input", "keyevent", "KEYCODE_BACK"], "keyevent", True),
    ([*S, "logcat", "-d", "-v", "threadtime", "-t", "200"], "logcat", False),
    ([*S, "logcat", "-d", "-v", "threadtime", "-t", "200", "--pid=4242"], "logcat_pid", False),
    (
        [
            *S,
            "shell",
            "cmd",
            "package",
            "resolve-activity",
            "--brief",
            "-c",
            "android.intent.category.LAUNCHER",
            "org.a.b",
        ],
        "resolve_launcher",
        False,
    ),
    (["-s", "192.168.1.20:5555", "shell", "pidof", "org.a.b"], "pidof", False),
]


@pytest.mark.parametrize(("args", "rule", "mutating"), ALLOWED)
def test_allowlisted_commands_match_expected_rule(
    args: list[str], rule: str, mutating: bool
) -> None:
    matched = validate_adb_args(args)
    assert matched.name == rule
    assert matched.mutating is mutating


def test_summary_lists_every_rule() -> None:
    summary = allowlist_summary()
    assert len(summary) == 13
    assert any("input_text" in line and "mutating" in line for line in summary)


def test_non_string_argument_rejected() -> None:
    with pytest.raises(PolicyViolation, match="strings"):
        validate_adb_args(["devices", 1])  # type: ignore[list-item]


def test_empty_command_rejected() -> None:
    with pytest.raises(PolicyViolation, match="empty"):
        validate_adb_args([])
