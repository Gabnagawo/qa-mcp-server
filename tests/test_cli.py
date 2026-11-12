from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from qa_mcp_server import cli
from qa_mcp_server.agent import AssistantTurn, Message, ToolCall, ToolSpec

from .conftest import APP


def test_allowlist_command(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["allowlist"]) == 0
    out = capsys.readouterr().out
    assert "input_text" in out
    assert "adb -s <serial> shell input tap <coord> <coord>" in out


def test_is_loopback() -> None:
    assert cli._is_loopback("127.0.0.1")
    assert cli._is_loopback("::1")
    assert cli._is_loopback("localhost")
    assert not cli._is_loopback("0.0.0.0")  # noqa: S104
    assert not cli._is_loopback("example.com")


def test_http_on_public_interface_needs_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        cli.main(["serve", "--transport", "http", "--host", "0.0.0.0"])  # noqa: S104
    assert "--allow-remote" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("argv", "transport"),
    [
        (["--fake"], "stdio"),
        ([], "stdio"),
        (["serve", "--fake", "--transport", "http", "--port", "9999"], "streamable-http"),
    ],
)
def test_serve_builds_server_and_runs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, argv: list[str], transport: str
) -> None:
    ran: dict[str, Any] = {}

    def fake_run(self: Any, transport: str = "stdio") -> None:
        ran["transport"] = transport
        ran["port"] = self.settings.port

    monkeypatch.setattr("qa_mcp_server.server.AuditedFastMCP.run", fake_run)
    monkeypatch.chdir(tmp_path)
    assert cli.main(argv) == 0
    assert ran["transport"] == transport
    if "--port" in argv:
        assert ran["port"] == 9999


class _Provider:
    name = "scripted"
    model = "m"

    def __init__(self) -> None:
        self.turns = [
            AssistantTurn(
                text="",
                tool_calls=[ToolCall(id="1", name="launch_app", arguments={"package": APP})],
            ),
            AssistantTurn(text="Done: app not installed.", tool_calls=[]),
        ]

    def chat(self, system: str, messages: list[Message], tools: list[ToolSpec]) -> AssistantTurn:
        return self.turns.pop(0)


def test_agent_command_with_fake_backend(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "provider_from_env", _Provider)
    code = cli.main(
        [
            "agent",
            "--fake",
            "--dry-run",
            "--artifacts-dir",
            str(tmp_path / "out"),
            "--apk-dir",
            str(tmp_path),
            "--goal",
            "try it",
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "provider=scripted model=m backend=fake" in out
    assert "[ERR] launch_app(package='org.qamcp.sample.notes')" in out
    assert "Done: app not installed." in out
    assert (tmp_path / "out" / "audit.jsonl").is_file()
