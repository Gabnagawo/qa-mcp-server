"""Server configuration.

Settings come from three layers, later layers win:

1. defaults defined on :class:`Settings`;
2. an optional TOML file (``--config qa-mcp.toml`` or ``QA_MCP_CONFIG``);
3. ``QA_MCP_*`` environment variables for the scalar options.

Test suites can only be declared in the TOML file: an agent can run a suite by
name, but it can never change what the suite executes.
"""

from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

SUITE_NAME_RE = r"^[a-z0-9][a-z0-9_-]{0,63}$"
PACKAGE_PATTERN_RE = r"^[A-Za-z0-9_.]+(\.\*)?$"

# Hard ceilings: configuration may lower limits but never raise them past these.
MAX_TIMEOUT_S = 3600.0
MAX_OUTPUT_BYTES = 64 * 1024 * 1024
MAX_LOGCAT_LINES = 5000


class TestSuiteConfig(BaseModel):
    """A test command the operator allowlisted. Agents select it by name only."""

    __test__ = False  # not a pytest test class

    model_config = ConfigDict(extra="forbid", frozen=True)

    argv: tuple[str, ...] = Field(min_length=1, description="Executable and arguments.")
    cwd: Path | None = Field(default=None, description="Working directory.")
    timeout_s: float = Field(default=900.0, gt=0, le=MAX_TIMEOUT_S)
    env: dict[str, str] = Field(default_factory=dict, description="Extra environment.")
    junit_xml: Path | None = Field(
        default=None, description="JUnit XML report written by the suite, relative to cwd."
    )
    description: str = ""

    @field_validator("argv")
    @classmethod
    def _no_empty_or_nul(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for arg in value:
            if not arg or "\x00" in arg:
                raise ValueError("argv entries must be non-empty and contain no NUL bytes")
        return value

    @field_validator("env")
    @classmethod
    def _env_names(cls, value: dict[str, str]) -> dict[str, str]:
        for key in value:
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                raise ValueError(f"invalid environment variable name: {key!r}")
        return value


class Settings(BaseModel):
    """Runtime settings. Every limit has a safe default."""

    model_config = ConfigDict(extra="forbid")

    backend: Literal["adb", "fake"] = "adb"
    adb_path: str = "adb"
    fake_scenario: Path | None = Field(
        default=None, description="Fake backend scenario JSON (defaults to the bundled one)."
    )
    apk_dir: Path = Field(default=Path("apks"), description="Only APKs inside this dir install.")
    artifacts_dir: Path = Field(default=Path("qa-mcp-artifacts"))
    audit_log: Path | None = Field(
        default=None, description="Defaults to artifacts_dir/audit.jsonl"
    )
    dry_run: bool = False
    redact_input_text: bool = True

    command_timeout_s: float = Field(default=30.0, gt=0, le=MAX_TIMEOUT_S)
    install_timeout_s: float = Field(default=180.0, gt=0, le=MAX_TIMEOUT_S)
    max_output_bytes: int = Field(default=4 * 1024 * 1024, gt=0, le=MAX_OUTPUT_BYTES)
    max_screenshot_bytes: int = Field(default=16 * 1024 * 1024, gt=0, le=MAX_OUTPUT_BYTES)
    max_apk_bytes: int = Field(default=512 * 1024 * 1024, gt=0)
    logcat_default_lines: int = Field(default=200, gt=0, le=MAX_LOGCAT_LINES)
    logcat_max_lines: int = Field(default=2000, gt=0, le=MAX_LOGCAT_LINES)
    logcat_max_bytes: int = Field(default=256 * 1024, gt=0, le=MAX_OUTPUT_BYTES)
    ui_max_nodes: int = Field(default=1500, gt=0, le=20000)
    suite_output_tail_bytes: int = Field(default=8 * 1024, gt=0, le=1024 * 1024)

    allowed_packages: tuple[str, ...] = Field(
        default=(),
        description="If set, only these packages (exact or 'prefix.*') can be launched/inspected.",
    )
    test_suites: dict[str, TestSuiteConfig] = Field(default_factory=dict)

    @field_validator("allowed_packages")
    @classmethod
    def _package_patterns(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for pattern in value:
            if not re.fullmatch(PACKAGE_PATTERN_RE, pattern):
                raise ValueError(f"invalid package pattern: {pattern!r}")
        return value

    @field_validator("test_suites")
    @classmethod
    def _suite_names(cls, value: dict[str, TestSuiteConfig]) -> dict[str, TestSuiteConfig]:
        for name in value:
            if not re.fullmatch(SUITE_NAME_RE, name):
                raise ValueError(f"invalid test suite name: {name!r}")
        return value

    @property
    def audit_log_path(self) -> Path:
        return self.audit_log or self.artifacts_dir / "audit.jsonl"

    @classmethod
    def load(
        cls,
        config_path: Path | None = None,
        env: Mapping[str, str] | None = None,
        **overrides: Any,
    ) -> Settings:
        """Build settings from TOML file, environment and explicit overrides."""
        env = os.environ if env is None else env
        data: dict[str, Any] = {}
        path = config_path or (Path(env["QA_MCP_CONFIG"]) if env.get("QA_MCP_CONFIG") else None)
        if path is not None:
            with path.open("rb") as fh:
                data.update(tomllib.load(fh))
            base = path.resolve().parent
            for key in ("apk_dir", "artifacts_dir", "audit_log", "fake_scenario"):
                if key in data and not Path(data[key]).is_absolute():
                    data[key] = str(base / data[key])
            for suite in data.get("test_suites", {}).values():
                if (
                    isinstance(suite, dict)
                    and suite.get("cwd")
                    and not Path(suite["cwd"]).is_absolute()
                ):
                    suite["cwd"] = str(base / suite["cwd"])
        data.update(_env_overrides(env))
        data.update({k: v for k, v in overrides.items() if v is not None})
        return cls.model_validate(data)


_ENV_KEYS = {
    "QA_MCP_BACKEND": "backend",
    "QA_MCP_ADB_PATH": "adb_path",
    "QA_MCP_APK_DIR": "apk_dir",
    "QA_MCP_ARTIFACTS_DIR": "artifacts_dir",
    "QA_MCP_AUDIT_LOG": "audit_log",
    "QA_MCP_FAKE_SCENARIO": "fake_scenario",
    "QA_MCP_DRY_RUN": "dry_run",
    "QA_MCP_COMMAND_TIMEOUT_S": "command_timeout_s",
}


def _env_overrides(env: Mapping[str, str]) -> dict[str, str]:
    return {field: env[var] for var, field in _ENV_KEYS.items() if env.get(var)}
