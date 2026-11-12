from __future__ import annotations

import shutil
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from mcp import ClientSession
from mcp.shared.memory import create_connected_server_and_client_session

from qa_mcp_server.audit import AuditLogger
from qa_mcp_server.config import Settings
from qa_mcp_server.fake import FakeAdbTransport
from qa_mcp_server.server import AuditedFastMCP, build_server, build_service
from qa_mcp_server.service import QAService

ROOT = Path(__file__).resolve().parents[1]
SAMPLE_APK = ROOT / "examples" / "apks" / "sample-notes-1.0.apk"
APP = "org.qamcp.sample.notes"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def apk_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "apks"
    directory.mkdir()
    shutil.copy(SAMPLE_APK, directory / SAMPLE_APK.name)
    return directory


@pytest.fixture
def settings(tmp_path: Path, apk_dir: Path) -> Settings:
    return Settings(backend="fake", apk_dir=apk_dir, artifacts_dir=tmp_path / "artifacts")


@pytest.fixture
def fake() -> FakeAdbTransport:
    return FakeAdbTransport.from_file()


@pytest.fixture
def service(settings: Settings, fake: FakeAdbTransport) -> QAService:
    return build_service(settings, transport=fake)


@pytest.fixture
def server(settings: Settings, service: QAService) -> AuditedFastMCP:
    return build_server(settings, service=service)


@pytest.fixture
def audit(server: AuditedFastMCP) -> AuditLogger:
    return server.audit


@pytest.fixture
async def client(server: AuditedFastMCP) -> AsyncIterator[ClientSession]:
    async with create_connected_server_and_client_session(server) as session:
        yield session


def mutating_calls(fake: FakeAdbTransport) -> list[tuple[str, ...]]:
    """Calls that would change device state (install, am start, input)."""
    markers = ({"install"}, {"am"}, {"input"})
    return [c for c in fake.calls if any(m & set(c) for m in markers)]
