"""Input validation: constrained types and request models for every tool.

Validation is deliberately strict. Several values end up as arguments of
``adb shell``, and adb joins those arguments with spaces and hands the result
to ``/system/bin/sh`` on the device. ``shell=False`` on the host therefore does
not stop injection on the device; only a strict character allowlist does.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from qa_mcp_server.errors import PolicyViolation

# Device serials: emulator-5554, R58M123ABC, 192.168.1.20:5555, adb-XYZ._adb-tls-connect._tcp
SERIAL_RE = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$"
# Java package names, at least two segments.
PACKAGE_RE = r"^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+$"
# Activity class: fully qualified or relative (".MainActivity"). No '$' (shell expansion).
ACTIVITY_RE = r"^\.?[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$"
# Logcat tags in the wild are mostly identifier-like; keep it strict.
TAG_RE = r"^[A-Za-z0-9_.\-]{1,64}$"
# Logcat 'since': [YYYY-]MM-DD HH:MM:SS[.mmm]
SINCE_RE = r"^(?:\d{4}-)?\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d{1,3})?$"
# Text typed via `input text`: printable ASCII minus every character that has a
# meaning for sh (quotes, ; & | $ ` \ < > ( ) { } [ ] * ? ~ # ! %) and no newlines.
INPUT_TEXT_RE = r"^[A-Za-z0-9 @._,:/+=-]{1,500}$"
# Every token passed after `adb shell` / `adb exec-out` must match this.
SAFE_SHELL_TOKEN_RE = r"^[A-Za-z0-9._/:=@,+%-]+$"  # noqa: S105 (regex, not a secret)
APK_FILENAME_RE = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,200}\.apk$"

MAX_COORD = 10_000

ALLOWED_KEYCODES: frozenset[str] = frozenset(
    {
        "HOME",
        "BACK",
        "MENU",
        "ENTER",
        "TAB",
        "SPACE",
        "DEL",
        "FORWARD_DEL",
        "ESCAPE",
        "SEARCH",
        "APP_SWITCH",
        "DPAD_UP",
        "DPAD_DOWN",
        "DPAD_LEFT",
        "DPAD_RIGHT",
        "DPAD_CENTER",
        "PAGE_UP",
        "PAGE_DOWN",
        "MOVE_HOME",
        "MOVE_END",
        "VOLUME_UP",
        "VOLUME_DOWN",
        "WAKEUP",
    }
)

Serial = Annotated[
    str,
    StringConstraints(pattern=SERIAL_RE),
    Field(description="Device serial from list_devices. Optional when one device is online."),
]
PackageName = Annotated[
    str,
    StringConstraints(pattern=PACKAGE_RE, max_length=255),
    Field(description="Android application id, e.g. org.example.app"),
]
ActivityName = Annotated[
    str,
    StringConstraints(pattern=ACTIVITY_RE, max_length=255),
    Field(description="Activity class, fully qualified or relative ('.MainActivity')."),
]
Coord = Annotated[int, Field(ge=0, le=MAX_COORD)]
LogLevel = Literal["V", "D", "I", "W", "E", "F"]
MatchMode = Literal["exact", "contains"]

_LEVEL_ORDER = "VDIWEF"


def level_at_least(level: str, minimum: str) -> bool:
    """True if logcat priority ``level`` is >= ``minimum`` (V < D < I < W < E < F)."""
    return _LEVEL_ORDER.index(level) >= _LEVEL_ORDER.index(minimum)


def _check_printable(value: str, field: str) -> None:
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ValueError(f"{field} must not contain control characters")


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class LaunchRequest(_Request):
    package: PackageName
    activity: ActivityName | None = None
    serial: Serial | None = None

    @property
    def component(self) -> str | None:
        if self.activity is None:
            return None
        return f"{self.package}/{self.activity}"


class InstallRequest(_Request):
    apk_path: Annotated[str, Field(min_length=1, max_length=4096)]
    serial: Serial | None = None

    @model_validator(mode="after")
    def _no_control_chars(self) -> InstallRequest:
        _check_printable(self.apk_path, "apk_path")
        return self


class LogcatRequest(_Request):
    serial: Serial | None = None
    package: PackageName | None = None
    tag: Annotated[str, StringConstraints(pattern=TAG_RE)] | None = None
    level: LogLevel = "V"
    since: Annotated[str, StringConstraints(pattern=SINCE_RE)] | None = None
    max_lines: Annotated[int, Field(ge=1, le=5000)] | None = None


class Selector(_Request):
    """Exactly one of text, resource_id or content_desc."""

    text: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    resource_id: Annotated[str, Field(min_length=1, max_length=255)] | None = None
    content_desc: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    match: MatchMode = "exact"
    index: Annotated[int, Field(ge=0, le=100)] = 0

    @model_validator(mode="after")
    def _exactly_one(self) -> Selector:
        given = [v for v in (self.text, self.resource_id, self.content_desc) if v is not None]
        if len(given) != 1:
            raise ValueError("selector needs exactly one of text, resource_id, content_desc")
        _check_printable(given[0], "selector")
        return self

    @property
    def field(self) -> Literal["text", "resource_id", "content_desc"]:
        if self.text is not None:
            return "text"
        if self.resource_id is not None:
            return "resource_id"
        return "content_desc"

    @property
    def value(self) -> str:
        return self.text or self.resource_id or self.content_desc or ""


class TapRequest(_Request):
    x: Coord | None = None
    y: Coord | None = None
    selector: Selector | None = None
    serial: Serial | None = None

    @model_validator(mode="after")
    def _coords_xor_selector(self) -> TapRequest:
        has_coords = self.x is not None or self.y is not None
        if has_coords and self.selector is not None:
            raise ValueError("pass either coordinates or a selector, not both")
        if has_coords and (self.x is None or self.y is None):
            raise ValueError("both x and y are required")
        if not has_coords and self.selector is None:
            raise ValueError("pass coordinates (x, y) or a selector")
        return self


class InputTextRequest(_Request):
    text: Annotated[str, StringConstraints(pattern=INPUT_TEXT_RE)]
    serial: Serial | None = None
    require_focus: bool = True

    @model_validator(mode="after")
    def _no_leading_dash(self) -> InputTextRequest:
        if self.text.startswith("-"):
            raise ValueError("text must not start with '-' (would be parsed as an option)")
        return self

    @property
    def encoded(self) -> str:
        """`input text` reads '%s' as a space; plain spaces would split the argument."""
        return self.text.replace(" ", "%s")


class PressKeyRequest(_Request):
    key: Annotated[str, Field(min_length=1, max_length=40)]
    serial: Serial | None = None

    @model_validator(mode="after")
    def _known_key(self) -> PressKeyRequest:
        name = self.key.upper().removeprefix("KEYCODE_")
        if name not in ALLOWED_KEYCODES:
            raise ValueError(f"key not allowed: {self.key!r}; allowed: {sorted(ALLOWED_KEYCODES)}")
        return self

    @property
    def keycode(self) -> str:
        return "KEYCODE_" + self.key.upper().removeprefix("KEYCODE_")


class ScreenshotRequest(_Request):
    serial: Serial | None = None
    save: bool = False


class DumpUiRequest(_Request):
    serial: Serial | None = None
    compact: bool = True


class RunSuiteRequest(_Request):
    name: Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")]
    serial: Serial | None = None


def resolve_apk_path(raw: str, apk_dir: Path, max_bytes: int) -> Path:
    """Resolve ``raw`` to a real .apk file strictly inside ``apk_dir``.

    Defends against ``..`` traversal, absolute paths elsewhere, symlinks that
    point outside the directory, non-APK files renamed to .apk, and file names
    that would need quoting on the device side.
    """
    if "\x00" in raw:
        raise PolicyViolation("apk_path contains a NUL byte")
    try:
        root = apk_dir.resolve(strict=True)
    except FileNotFoundError as exc:
        raise PolicyViolation(f"APK directory does not exist: {apk_dir}") from exc
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        resolved = candidate.resolve(strict=True)
    except (FileNotFoundError, OSError) as exc:
        raise PolicyViolation("apk_path does not exist inside the APK directory") from exc
    if not resolved.is_relative_to(root):
        raise PolicyViolation("apk_path escapes the allowed APK directory")
    if not resolved.is_file():
        raise PolicyViolation("apk_path is not a regular file")
    if not re.fullmatch(APK_FILENAME_RE, resolved.name, flags=re.IGNORECASE):
        raise PolicyViolation("APK file name must be [A-Za-z0-9._-] and end in .apk")
    size = resolved.stat().st_size
    if size > max_bytes:
        raise PolicyViolation(f"APK is larger than the {max_bytes} byte limit")
    with resolved.open("rb") as fh:
        if fh.read(4) != b"PK\x03\x04":
            raise PolicyViolation("file is not an APK (missing ZIP signature)")
    return resolved
