"""Process execution with hard timeouts and output caps.

:class:`SubprocessTransport` is the only place in the package that starts a
process. It always passes an argument list with ``shell=False``, closes stdin,
reads stdout/stderr concurrently and kills the process, with every process it
started, when it exceeds either its timeout or its output cap, so a runaway
command can neither hang the server nor exhaust its memory.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Protocol

from qa_mcp_server.errors import CommandFailed, CommandTimeout

STDERR_CAP = 64 * 1024
_CHUNK = 64 * 1024
# How long to wait for the pipes to close after the process is gone. A descendant
# that survived the kill can keep a pipe open; the call must still return.
_DRAIN_S = 5.0
_KILL_TOOL_S = 10.0


@dataclass(frozen=True)
class RunResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: bytes
    stderr: bytes
    duration_s: float
    truncated: bool = False
    dry_run: bool = False

    @property
    def text(self) -> str:
        return self.stdout.decode("utf-8", errors="replace")

    @property
    def error_text(self) -> str:
        return self.stderr.decode("utf-8", errors="replace").strip()


class Transport(Protocol):
    """Runs an argv. Implementations: subprocess (real) and the fake device."""

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout_s: float,
        max_bytes: int,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> RunResult: ...


class _CappedReader(threading.Thread):
    def __init__(self, stream: IO[bytes], cap: int, on_overflow: threading.Event) -> None:
        super().__init__(daemon=True)
        self._stream = stream
        self._cap = cap
        self._on_overflow = on_overflow
        self.buffer = bytearray()
        self.truncated = False

    def run(self) -> None:
        while chunk := self._stream.read(_CHUNK):
            room = self._cap - len(self.buffer)
            if len(chunk) > room:
                self.buffer.extend(chunk[: max(room, 0)])
                self.truncated = True
                self._on_overflow.set()
                # Leave the pipe open: closing it here would let the child die of a
                # broken pipe before its process tree is killed, orphaning the rest.
                return
            self.buffer.extend(chunk)
        self._stream.close()


def _kill_tree(proc: subprocess.Popen[bytes]) -> None:
    """Kill ``proc`` and the processes it started.

    A test suite may be a wrapper (a shell script, gradle, pytest-xdist). Killing only
    the direct child leaves the rest running and holding the output pipes open.
    """
    if sys.platform == "win32":
        # taskkill walks the tree by parent PID, so it has to run while the parent
        # is still alive, before proc.kill().
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(  # fixed argv, no shell
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)],  # noqa: S607
                shell=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=_KILL_TOOL_S,
                check=False,
            )
    else:  # pragma: no cover (POSIX branch; the tests run it on the CI runner)
        # The child was started as the leader of its own session and process group.
        with contextlib.suppress(OSError):
            os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(OSError):
        proc.kill()


class SubprocessTransport:
    """Real process execution. ``shell=False`` always; never a command string."""

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout_s: float,
        max_bytes: int,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> RunResult:
        if isinstance(argv, str) or not argv:
            raise TypeError("argv must be a non-empty sequence of strings")
        args = tuple(argv)
        full_env = None if env is None else {**os.environ, **env}
        start = time.monotonic()
        try:
            proc = subprocess.Popen(
                args,
                shell=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=cwd,
                env=full_env,
                # POSIX: own session, so the whole group can be killed on timeout.
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            raise CommandFailed(f"executable not found: {args[0]}") from exc
        assert proc.stdout is not None  # noqa: S101 (PIPE set above)
        assert proc.stderr is not None  # noqa: S101

        overflow = threading.Event()
        out = _CappedReader(proc.stdout, max_bytes, overflow)
        err = _CappedReader(proc.stderr, STDERR_CAP, threading.Event())
        out.start()
        err.start()

        deadline = start + timeout_s
        while proc.poll() is None:
            if overflow.is_set():
                _kill_tree(proc)
                break
            if time.monotonic() >= deadline:
                _kill_tree(proc)
                proc.wait()
                out.join(1)
                err.join(1)
                raise CommandTimeout(f"{Path(args[0]).name} timed out after {timeout_s:g}s")
            overflow.wait(0.01)
        returncode = proc.wait()
        out.join(_DRAIN_S)
        err.join(_DRAIN_S)
        if out.truncated:
            proc.stdout.close()
        return RunResult(
            argv=args,
            returncode=returncode,
            stdout=bytes(out.buffer),
            stderr=bytes(err.buffer),
            duration_s=time.monotonic() - start,
            truncated=out.truncated,
        )
