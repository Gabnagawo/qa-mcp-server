# Linux/macOS/CI. On Windows run the `uv run ...` commands shown in the README.
.PHONY: install lint format typecheck test security demo bench agent allowlist docker clean

install:
	uv sync

lint:
	uv run ruff check .
	uv run ruff format --check .

format:
	uv run ruff format .
	uv run ruff check --fix .

typecheck:
	uv run mypy

test:
	uv run pytest

security:
	uv run pytest tests/test_security_matrix.py -q --no-cov

demo:
	uv run python examples/demo_transcript.py

bench:
	uv run python examples/benchmark_latency.py --iterations 200

# Needs Ollama running locally with qwen3:8b (or LLM_PROVIDER/LLM_MODEL set).
agent:
	uv run qa-mcp-server agent --fake --apk-dir examples/apks --artifacts-dir qa-mcp-artifacts/agent

allowlist:
	uv run qa-mcp-server allowlist

docker:
	docker build -t qa-mcp-server .

clean:
	rm -rf qa-mcp-artifacts .pytest_cache .ruff_cache .mypy_cache .coverage coverage.xml htmlcov
