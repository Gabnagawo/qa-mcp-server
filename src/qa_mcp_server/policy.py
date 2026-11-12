"""The adb command allowlist.

Every adb invocation is checked here immediately before it reaches a
transport. An argv is accepted only if it matches one rule token by token:
literal tokens must be equal and placeholder tokens must match their regex or
predicate. Anything else (extra flags, unknown subcommands, shell
metacharacters) raises :class:`PolicyViolation`.

Rules carry a ``mutating`` flag; in dry-run mode mutating commands are planned
and audited but not executed.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath

from qa_mcp_server.errors import PolicyViolation
from qa_mcp_server.validation import (
    ACTIVITY_RE,
    ALLOWED_KEYCODES,
    APK_FILENAME_RE,
    MAX_COORD,
    PACKAGE_RE,
    SAFE_SHELL_TOKEN_RE,
    SERIAL_RE,
)


@dataclass(frozen=True)
class Arg:
    """Placeholder token validated by a predicate."""

    name: str
    check: Callable[[str], bool]


def _re(pattern: str) -> Callable[[str], bool]:
    compiled = re.compile(pattern)
    return lambda value: compiled.fullmatch(value) is not None


def _coord(value: str) -> bool:
    return value.isascii() and value.isdigit() and int(value) <= MAX_COORD


def _component(value: str) -> bool:
    package, sep, activity = value.partition("/")
    return bool(sep) and _re(PACKAGE_RE)(package) and _re(ACTIVITY_RE)(activity)


def _keycode(value: str) -> bool:
    return value.startswith("KEYCODE_") and value.removeprefix("KEYCODE_") in ALLOWED_KEYCODES


def _host_apk_path(value: str) -> bool:
    """Absolute host path to an .apk (the path sandbox is enforced earlier)."""
    if any(ch in value for ch in "\x00\r\n"):
        return False
    for flavour in (PurePosixPath, PureWindowsPath):
        path = flavour(value)
        if path.is_absolute():
            return re.fullmatch(APK_FILENAME_RE, path.name, flags=re.IGNORECASE) is not None
    return False


def _line_count(value: str) -> bool:
    return value.isascii() and value.isdigit() and 1 <= int(value) <= 100_000


SERIAL = Arg("serial", _re(SERIAL_RE))
PACKAGE = Arg("package", _re(PACKAGE_RE))
COMPONENT = Arg("component", _component)
COORD = Arg("coord", _coord)
KEYCODE = Arg("keycode", _keycode)
TEXT = Arg("text", _re(r"^[A-Za-z0-9@._,:/+=%-]{1,1000}$"))
APK = Arg("apk", _host_apk_path)
LINES = Arg("lines", _line_count)
PID_FILTER = Arg("pid", _re(r"^--pid=[1-9][0-9]{0,6}$"))

Token = str | Arg


@dataclass(frozen=True)
class CommandRule:
    name: str
    pattern: tuple[Token, ...]
    mutating: bool

    def matches(self, args: Sequence[str]) -> bool:
        if len(args) != len(self.pattern):
            return False
        for token, arg in zip(self.pattern, args, strict=True):
            if isinstance(token, str):
                if token != arg:
                    return False
            elif not token.check(arg):
                return False
        return True


GLOBAL_RULES: tuple[CommandRule, ...] = (CommandRule("devices", ("devices", "-l"), mutating=False),)

# Matched against the arguments that follow ``-s <serial>``.
DEVICE_RULES: tuple[CommandRule, ...] = (
    CommandRule("install", ("install", "-r", APK), mutating=True),
    CommandRule("am_start", ("shell", "am", "start", "-W", "-n", COMPONENT), mutating=True),
    CommandRule("pm_path", ("shell", "pm", "path", PACKAGE), mutating=False),
    CommandRule(
        "resolve_launcher",
        (
            "shell",
            "cmd",
            "package",
            "resolve-activity",
            "--brief",
            "-c",
            "android.intent.category.LAUNCHER",
            PACKAGE,
        ),
        mutating=False,
    ),
    CommandRule("pidof", ("shell", "pidof", PACKAGE), mutating=False),
    CommandRule("screencap", ("exec-out", "screencap", "-p"), mutating=False),
    CommandRule("ui_dump", ("exec-out", "uiautomator", "dump", "/dev/tty"), mutating=False),
    CommandRule("tap", ("shell", "input", "tap", COORD, COORD), mutating=True),
    CommandRule("input_text", ("shell", "input", "text", TEXT), mutating=True),
    CommandRule("keyevent", ("shell", "input", "keyevent", KEYCODE), mutating=True),
    CommandRule("logcat", ("logcat", "-d", "-v", "threadtime", "-t", LINES), mutating=False),
    CommandRule(
        "logcat_pid",
        ("logcat", "-d", "-v", "threadtime", "-t", LINES, PID_FILTER),
        mutating=False,
    ),
)

_SAFE_SHELL_TOKEN = re.compile(SAFE_SHELL_TOKEN_RE)
_DEVICE_SHELL_SUBCOMMANDS = frozenset({"shell", "exec-out"})


def validate_adb_args(args: Sequence[str]) -> CommandRule:
    """Return the matching rule for ``adb <args>`` or raise :class:`PolicyViolation`."""
    if not args:
        raise PolicyViolation("empty adb command")
    for arg in args:
        if not isinstance(arg, str):
            raise PolicyViolation("adb arguments must be strings")
        if any(ch in arg for ch in "\x00\r\n"):
            raise PolicyViolation("adb arguments must not contain NUL or newlines")

    if args[0] == "-s":
        if len(args) < 3 or not SERIAL.check(args[1]):
            raise PolicyViolation("invalid device serial")
        rest = args[2:]
        # Defense in depth: anything that reaches the device shell must be a plain token,
        # whatever rule it matches.
        if rest[0] in _DEVICE_SHELL_SUBCOMMANDS:
            for token in rest[1:]:
                if not _SAFE_SHELL_TOKEN.fullmatch(token):
                    raise PolicyViolation(f"unsafe token for device shell: {token!r}")
        rules: Sequence[CommandRule] = DEVICE_RULES
    else:
        rest = args
        rules = GLOBAL_RULES

    for rule in rules:
        if rule.matches(rest):
            return rule
    raise PolicyViolation(f"adb command not on the allowlist: {' '.join(args[:4])!r}...")


def allowlist_summary() -> list[str]:
    """Human-readable list of permitted command shapes (used in docs and tests)."""
    lines = []
    for rule in (*GLOBAL_RULES, *DEVICE_RULES):
        prefix = "adb" if rule in GLOBAL_RULES else "adb -s <serial>"
        shape = " ".join(t if isinstance(t, str) else f"<{t.name}>" for t in rule.pattern)
        lines.append(
            f"{rule.name:<17} {'mutating ' if rule.mutating else 'read-only'}  {prefix} {shape}"
        )
    return lines
