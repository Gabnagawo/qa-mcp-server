# qa-mcp-server: MCP server exposing allowlisted Android QA tools over adb.
#
#   docker build -t qa-mcp-server .
#   # fake device, stdio (what an MCP client launches):
#   docker run --rm -i qa-mcp-server serve --fake
#   # real devices through the host's adb server:
#   docker run --rm -i -e ADB_SERVER_SOCKET=tcp:host.docker.internal:5037 \
#     -v "$PWD/apks:/data/apks:ro" qa-mcp-server serve --apk-dir /data/apks
FROM python:3.12-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.12.10 /uv /uvx /bin/

# hadolint ignore=DL3008
RUN apt-get update \
    && apt-get install -y --no-install-recommends adb \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 qa

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
COPY examples ./examples
RUN uv sync --locked --no-dev && chown -R qa:qa /app

USER qa
ENV PATH="/app/.venv/bin:$PATH" \
    QA_MCP_ARTIFACTS_DIR=/home/qa/artifacts

ENTRYPOINT ["qa-mcp-server"]
CMD ["serve"]
