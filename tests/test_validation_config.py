from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from qa_mcp_server.config import Settings, TestSuiteConfig
from qa_mcp_server.errors import PolicyViolation
from qa_mcp_server.validation import (
    InputTextRequest,
    LaunchRequest,
    PressKeyRequest,
    Selector,
    TapRequest,
    level_at_least,
    resolve_apk_path,
)


def test_input_text_encodes_spaces() -> None:
    assert InputTextRequest(text="buy milk now").encoded == "buy%smilk%snow"


def test_press_key_normalises_names() -> None:
    assert PressKeyRequest(key="back").keycode == "KEYCODE_BACK"
    assert PressKeyRequest(key="KEYCODE_ENTER").keycode == "KEYCODE_ENTER"


def test_launch_component() -> None:
    assert LaunchRequest(package="org.a.b").component is None
    assert LaunchRequest(package="org.a.b", activity=".Main").component == "org.a.b/.Main"


@pytest.mark.parametrize(
    ("kwargs", "field", "value"),
    [
        ({"text": "Save"}, "text", "Save"),
        ({"resource_id": "save_button"}, "resource_id", "save_button"),
        ({"content_desc": "New note"}, "content_desc", "New note"),
    ],
)
def test_selector_field(kwargs: dict[str, str], field: str, value: str) -> None:
    selector = Selector(**kwargs)  # type: ignore[arg-type]
    assert selector.field == field
    assert selector.value == value


def test_selector_needs_exactly_one() -> None:
    with pytest.raises(ValidationError):
        Selector()


def test_tap_requires_coords_or_selector() -> None:
    with pytest.raises(ValidationError, match="coordinates"):
        TapRequest()
    assert TapRequest(x=1, y=2).selector is None


def test_level_order() -> None:
    assert level_at_least("E", "W")
    assert not level_at_least("D", "I")


def test_resolve_apk_path_ok(apk_dir: Path) -> None:
    resolved = resolve_apk_path("sample-notes-1.0.apk", apk_dir, 10_000)
    assert resolved.name == "sample-notes-1.0.apk"
    assert resolve_apk_path(str(resolved), apk_dir, 10_000) == resolved


def test_resolve_apk_path_size_cap(apk_dir: Path) -> None:
    with pytest.raises(PolicyViolation, match="larger"):
        resolve_apk_path("sample-notes-1.0.apk", apk_dir, 10)


def test_resolve_apk_path_missing_dir(tmp_path: Path) -> None:
    with pytest.raises(PolicyViolation, match="does not exist"):
        resolve_apk_path("x.apk", tmp_path / "nope", 10)


def test_resolve_apk_path_directory(apk_dir: Path) -> None:
    (apk_dir / "dir.apk").mkdir()
    with pytest.raises(PolicyViolation, match="regular file"):
        resolve_apk_path("dir.apk", apk_dir, 10_000)


# -- config --------------------------------------------------------------------------


def test_settings_defaults() -> None:
    settings = Settings()
    assert settings.backend == "adb"
    assert settings.audit_log_path == Path("qa-mcp-artifacts") / "audit.jsonl"


def test_settings_load_toml_env_and_overrides(tmp_path: Path) -> None:
    config = tmp_path / "qa-mcp.toml"
    config.write_text(
        """
apk_dir = "apks"
allowed_packages = ["org.qamcp.*"]
command_timeout_s = 12

[test_suites.smoke]
argv = ["python", "-c", "print('ok')"]
cwd = "suites"
timeout_s = 60
"""
    )
    settings = Settings.load(config, env={"QA_MCP_DRY_RUN": "true"}, backend="fake")
    assert settings.apk_dir == tmp_path / "apks"
    assert settings.dry_run is True
    assert settings.backend == "fake"
    assert settings.command_timeout_s == 12
    suite = settings.test_suites["smoke"]
    assert suite.cwd == tmp_path / "suites"
    assert suite.argv[0] == "python"


def test_settings_load_from_env_config(tmp_path: Path) -> None:
    config = tmp_path / "c.toml"
    out = (tmp_path / "out").as_posix()
    config.write_text(f'artifacts_dir = "{out}"\n')
    settings = Settings.load(env={"QA_MCP_CONFIG": str(config)})
    assert settings.artifacts_dir == Path(out)


@pytest.mark.parametrize(
    "data",
    [
        {"allowed_packages": ["org.*.evil"]},
        {"test_suites": {"Bad Name": {"argv": ["x"]}}},
        {"test_suites": {"ok": {"argv": []}}},
        {"test_suites": {"ok": {"argv": ["a\x00b"]}}},
        {"test_suites": {"ok": {"argv": ["x"], "env": {"BAD-NAME": "1"}}}},
        {"command_timeout_s": 10_000},
        {"unknown_option": True},
    ],
)
def test_settings_reject_invalid(data: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate(data)


def test_suite_config_is_not_collected_as_test() -> None:
    assert TestSuiteConfig.__test__ is False
