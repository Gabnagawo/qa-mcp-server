"""Command line entry point: ``qa-mcp-server [serve|agent|allowlist]``."""

from __future__ import annotations

import argparse
import ipaddress
import sys
from collections.abc import Sequence
from pathlib import Path

import anyio
from mcp.shared.memory import create_connected_server_and_client_session

from qa_mcp_server import __version__
from qa_mcp_server.agent import StepRecord, explore, provider_from_env
from qa_mcp_server.config import Settings
from qa_mcp_server.policy import allowlist_summary
from qa_mcp_server.server import build_server

DEFAULT_GOAL = (
    "Exploratory test of the app org.qamcp.sample.notes. Install sample-notes-1.0.apk, "
    "launch the app, create a note titled 'Groceries' and save it, then open a new note "
    "and try the 'Export as PDF' action. After each risky action check logcat for errors. "
    "Finish with a short bug report."
)


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", type=Path, help="TOML config file (see qa-mcp.example.toml)")
    parser.add_argument("--fake", action="store_true", help="use the scripted fake device")
    parser.add_argument("--scenario", type=Path, help="fake device scenario JSON")
    parser.add_argument("--dry-run", action="store_true", help="plan mutating commands only")
    parser.add_argument("--apk-dir", type=Path, help="directory APKs may be installed from")
    parser.add_argument("--artifacts-dir", type=Path, help="screenshots and audit log")


def _settings(args: argparse.Namespace) -> Settings:
    return Settings.load(
        args.config,
        backend="fake" if args.fake else None,
        fake_scenario=args.scenario,
        dry_run=True if args.dry_run else None,
        apk_dir=args.apk_dir,
        artifacts_dir=args.artifacts_dir,
    )


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="qa-mcp-server", description=__doc__)
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="run the MCP server (default)")
    _add_common(serve)
    serve.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument(
        "--allow-remote",
        action="store_true",
        help="allow binding HTTP to a non-loopback address (no auth: put it behind a proxy)",
    )

    agent = sub.add_parser("agent", help="drive the server with an LLM (LLM_PROVIDER, LLM_MODEL)")
    _add_common(agent)
    agent.add_argument("--goal", default=DEFAULT_GOAL)
    agent.add_argument("--max-steps", type=int, default=20)

    sub.add_parser("allowlist", help="print the adb command allowlist")
    return parser


def _print_step(step: StepRecord) -> None:
    status = "ERR" if step.is_error else "ok "
    args = ", ".join(f"{k}={v!r}" for k, v in step.arguments.items())
    preview = step.result_preview.replace("\n", " ")[:160]
    print(f"  [{status}] {step.tool}({args}) -> {preview}", flush=True)


async def _run_agent(args: argparse.Namespace) -> int:
    settings = _settings(args)
    provider = provider_from_env()
    server = build_server(settings)
    print(f"provider={provider.name} model={provider.model} backend={settings.backend}")
    async with create_connected_server_and_client_session(server) as session:
        report = await explore(
            session, provider, args.goal, max_steps=args.max_steps, on_step=_print_step
        )
    print(f"\nstopped: {report.stopped_reason} after {len(report.steps)} tool calls")
    print("\n" + (report.final_answer or "(no final answer)"))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    if not raw or raw[0] not in {"serve", "agent", "allowlist", "-h", "--help", "--version"}:
        raw = ["serve", *raw]  # bare `qa-mcp-server [options]` means `serve`
    parser = build_parser()
    args = parser.parse_args(raw)
    if args.command == "allowlist":
        print("
".join(allowlist_summary()))
        return 0
    if args.command == "agent":
        return anyio.run(_run_agent, args)
    if args.transport == "http" and not _is_loopback(args.host) and not args.allow_remote:
        parser.error("refusing to bind a non-loopback host without --allow-remote")
    server = build_server(_settings(args), host=args.host, port=args.port)
    server.run("streamable-http" if args.transport == "http" else "stdio")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
