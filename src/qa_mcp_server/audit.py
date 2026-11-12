"""Append-only JSONL audit log of every tool call and every command it planned.

Each line records the tool, its (sanitised) arguments, the outcome
(``ok`` / ``rejected`` / ``error``), the latency and the exact argv of each
adb or test command, including whether it actually executed (dry-run).
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

MAX_AUDIT_STRING = 300


@dataclass(frozen=True)
class CommandRecord:
    rule: str
    argv: tuple[str, ...]
    executed: bool


_commands: ContextVar[list[CommandRecord] | None] = ContextVar("qa_mcp_commands", default=None)


def record_command(rule: str, argv: tuple[str, ...], executed: bool) -> None:
    """Attach a planned/executed command to the tool call in progress (if any)."""
    bucket = _commands.get()
    if bucket is not None:
        bucket.append(CommandRecord(rule=rule, argv=argv, executed=executed))


@contextmanager
def capture_commands() -> Iterator[list[CommandRecord]]:
    bucket: list[CommandRecord] = []
    token = _commands.set(bucket)
    try:
        yield bucket
    finally:
        _commands.reset(token)


def sanitize_arguments(
    tool: str, arguments: Mapping[str, Any], *, redact_input_text: bool
) -> dict[str, Any]:
    """Truncate long values and redact typed text so secrets don't land in the log."""
    clean: dict[str, Any] = {}
    for key, value in arguments.items():
        if tool == "input_text" and key == "text" and redact_input_text and isinstance(value, str):
            clean[key] = f"<redacted {len(value)} chars>"
        elif isinstance(value, str) and len(value) > MAX_AUDIT_STRING:
            clean[key] = value[:MAX_AUDIT_STRING] + f"...<{len(value)} chars>"
        elif isinstance(value, str | int | float | bool) or value is None:
            clean[key] = value
        else:
            text = json.dumps(value, default=str)
            clean[key] = value if len(text) <= MAX_AUDIT_STRING else text[:MAX_AUDIT_STRING] + "..."
    return clean


class AuditLogger:
    """Thread-safe JSONL writer. One line per tool call, flushed immediately."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)

    def write(
        self,
        *,
        tool: str,
        arguments: dict[str, Any],
        outcome: str,
        duration_ms: float,
        dry_run: bool,
        commands: list[CommandRecord],
        error: str | None = None,
    ) -> dict[str, Any]:
        record: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "tool": tool,
            "arguments": arguments,
            "outcome": outcome,
            "duration_ms": round(duration_ms, 3),
            "dry_run": dry_run,
            "commands": [asdict(c) for c in commands],
        }
        if error is not None:
            record["error"] = error[:1000]
        line = json.dumps(record, ensure_ascii=False, default=str)
        with self._lock, self.path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        return record

    def read(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        with self.path.open(encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]
