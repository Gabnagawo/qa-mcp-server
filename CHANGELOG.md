# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [0.1.0] - 2026-11-12

### Added
- MCP server (official `mcp` SDK, FastMCP) over stdio, with optional streamable HTTP bound to loopback.
- Ten Android QA tools: `list_devices`, `install_apk`, `launch_app`, `screenshot`, `logcat`,
  `dump_ui`, `tap` (by coordinates or by text / resource-id / content-desc selector),
  `input_text`, `press_key`, `run_test_suite`.
- Security controls: pydantic validation of every argument, a 13-rule adb argv allowlist checked
  right before execution, `shell=False` everywhere, a device-shell-safe token rule, an APK path
  sandbox, a device serial check against `adb devices`, a package allowlist, operator-defined test
  suites, timeouts and output caps, `defusedxml` for UI dumps, dry-run mode and a JSONL audit log.
- `input_text` fails when no field has input focus (opt out with `require_focus=false`), so agents
  cannot report text that was never typed.
- Scripted fake device (`--fake`) that answers the adb argv protocol from a JSON scenario:
  installed packages, UI hierarchy fixtures with tap transitions, logcat lines, a PNG screenshot
  and a planted crash in a synthetic notes app.
- Reference agent loop (`qa-mcp-server agent`) with Ollama (default `qwen3:8b`), Anthropic and
  OpenAI providers selected through `LLM_PROVIDER` / `LLM_MODEL`. The Ollama provider sets
  `num_ctx` (default 16384), sends compact JSON tool results and retries HTTP 5xx twice with a new seed per attempt.
- Security test matrix (tool layer through a real MCP client, plus the raw command layer),
  in-process MCP client tests, a stdio demo transcript and a latency benchmark.
- Docs: threat model (`docs/SECURITY.md`), real-device demo script for Markor from F-Droid
  (`docs/DEMO.md`), Claude Desktop and Claude Code setup.
- CI (ruff, mypy, pytest with coverage gate, demo, hadolint), Dockerfile and Makefile.

[0.1.0]: https://github.com/Gabnagawo/qa-mcp-server/releases/tag/v0.1.0
