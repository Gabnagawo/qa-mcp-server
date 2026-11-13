"""Real-process tests for SubprocessTransport (uses the current Python as the child)."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

from qa_mcp_server.errors import CommandFailed, CommandTimeout
from qa_mcp_server.transport import SubprocessTransport

PY = sys.executable


def run(code: str, *extra: str, timeout_s: float = 10, max_bytes: int = 1_000_000, **kw: object):  # type: ignore[no-untyped-def]
    return SubprocessTransport().run(
        [PY, "-c", code, *extra],
        timeout_s=timeout_s,
        max_bytes=max_bytes,
        **kw,  # type: ignore[arg-type]
    )


def test_captures_stdout_stderr_and_exit_code() -> None:
    result = run("import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)")
    assert result.returncode == 3
    assert result.text.strip() == "out"
    assert result.error_text == "err"
    assert not result.truncated


@pytest.mark.security
def test_shell_metacharacters_are_literal_arguments() -> None:
    payload = "; echo PWNED && $(whoami) | cat `id` > /tmp/x"
    result = run("import sys, json; print(json.dumps(sys.argv[1:]))", payload)
    assert json.loads(result.text) == [payload]
    assert "PWNED\n" not in result.text.replace(payload, "")


@pytest.mark.security
def test_timeout_kills_process() -> None:
    with pytest.raises(CommandTimeout, match="timed out"):
        run("import time; time.sleep(30)", timeout_s=0.5)


# A child that starts a grandchild. The grandchild inherits the output pipes,
# never exits and appends to the file named in argv[1] every 50 ms.
_SPAWN_TICKER = """
import subprocess, sys, time
ticker = (
    "import sys, time\\n"
    "while True:\\n"
    "    open(sys.argv[1], 'a').write('x')\\n"
    "    time.sleep(0.05)\\n"
)
subprocess.Popen([sys.executable, "-c", ticker, sys.argv[1]])
"""


def _wait_for_ticks(path: Path) -> None:
    deadline = time.monotonic() + 10
    while not path.exists() or path.stat().st_size == 0:
        assert time.monotonic() < deadline, "the grandchild never started"
        time.sleep(0.05)


def _assert_ticker_stopped(path: Path) -> None:
    time.sleep(0.5)
    size = path.stat().st_size
    time.sleep(0.5)
    assert path.stat().st_size == size, "the grandchild is still running"


@pytest.mark.security
def test_timeout_kills_the_whole_process_tree(tmp_path: Path) -> None:
    ticks = tmp_path / "ticks"
    started = time.monotonic()
    with pytest.raises(CommandTimeout, match="timed out"):
        run(_SPAWN_TICKER + "time.sleep(60)\n", str(ticks), timeout_s=3)
    assert time.monotonic() - started < 20
    _wait_for_ticks(ticks)
    _assert_ticker_stopped(ticks)


@pytest.mark.security
def test_output_cap_kills_the_whole_process_tree(tmp_path: Path) -> None:
    ticks = tmp_path / "ticks"
    flood = "time.sleep(1)\nwhile True: sys.stdout.write('x' * 65536)\n"
    result = run(_SPAWN_TICKER + flood, str(ticks), max_bytes=100_000, timeout_s=30)
    assert result.truncated
    _wait_for_ticks(ticks)
    _assert_ticker_stopped(ticks)


@pytest.mark.security
def test_output_cap_truncates_and_kills() -> None:
    result = run(
        "import sys\nwhile True: sys.stdout.write('x' * 65536)",
        max_bytes=100_000,
        timeout_s=20,
    )
    assert result.truncated
    assert len(result.stdout) == 100_000


def test_cwd_and_env(tmp_path: Path) -> None:
    result = run(
        "import os; print(os.getcwd()); print(os.environ['QA_X'])",
        cwd=tmp_path,
        env={"QA_X": "42"},
    )
    lines = result.text.splitlines()
    assert Path(lines[0]).resolve() == tmp_path.resolve()
    assert lines[1] == "42"


def test_missing_executable() -> None:
    with pytest.raises(CommandFailed, match="not found"):
        SubprocessTransport().run(["definitely-not-a-real-binary-xyz"], timeout_s=5, max_bytes=10)


@pytest.mark.security
def test_string_argv_refused() -> None:
    with pytest.raises(TypeError):
        SubprocessTransport().run("echo hi", timeout_s=5, max_bytes=10)
