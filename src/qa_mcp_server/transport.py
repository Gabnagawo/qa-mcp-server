"""Process execution with hard timeouts and output caps.

:class:`SubprocessTransport` is the only place in the package that starts a
process. It always passes an argument list with ``shell=False``, closes stdin,
reads stdout/stderr concurrently and kills the process when it exceeds either
its timeout or its output cap, so a runaway command can neither hang the
server nor exhaust its memory.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Protocol

from qa_mcp_server.errors import CommandFailed, CommandTimeout

STDERR_CAP = 64 * 1024
_CHUNK = 64 * 1024


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
                break
            self.buffer.extend(chunk)
        self._stream.close()


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
            )
        except FileNotFoundError as exc:
            raise CommandFailed(f"executable not found: {args[0]}") from exc
        assert proc.stdout is not None and proc.stderr is not None  # noqa: S101 (PIPE set above)

        overflow = threading.Event()
        out = _CappedReader(proc.stdout, max_bytes, overflow)
        err = _CappedReader(proc.stderr, STDERR_CAP, threading.Event())
        out.start()
        err.start()

        deadline = start + timeout_s
        while proc.poll() is None:
            if overflow.is_set():
                proc.kill()
                break
            if time.monotonic() >= deadline:
                proc.kill()
                proc.wait()
                out.join(1)
                err.join(1)
                raise CommandTimeout(f"{Path(args[0]).name} timed out after {timeout_s:g}s")
            overflow.wait(0.01)
        returncode = proc.wait()
        out.join()
        err.join()
        return RunResult(
            argv=args,
            returncode=returncode,
            stdout=bytes(out.buffer),
            stderr=bytes(err.buffer),
            duration_s=time.monotonic() - start,
            truncated=out.truncated,
        )
