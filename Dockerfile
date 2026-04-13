# Minimal Dockerfile for Glama MCP server validation.
#
# Glama (https://glama.ai/mcp/servers) runs this container and talks to the
# Cortex MCP server over stdio, checking that it starts and responds to
# `tools/list`. It does NOT need the miner daemon, the real Obsidian vault,
# or the `claude` CLI — the server gracefully degrades when those are absent.
#
# This is NOT how normal users install Cortex. End users install via
# `/plugin install cortex` which triggers the bootstrap.sh shim on first
# spawn (see README for the real install path).

FROM python:3.11-slim

# uv for fast deterministic installs (matches bootstrap.sh's strategy)
RUN pip install --no-cache-dir uv==0.5.* \
 && apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy project metadata first so Docker's layer cache can reuse the
# dep-sync layer when only source files change.
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY cortex_server ./cortex_server

# Install deps into a plugin-local venv, frozen against uv.lock.
# --no-dev skips pytest/ruff/mypy which aren't needed to run the server.
RUN uv sync --frozen --no-dev

# Sandbox-friendly defaults. Glama's build sandbox is ephemeral so we
# point Cortex at writable /tmp paths instead of ~/.cortex / ~/obsidian-brain.
ENV CORTEX_DIR=/tmp/.cortex \
    CORTEX_OBSIDIAN_VAULT=/tmp/obsidian-brain \
    PYTHONUNBUFFERED=1

RUN mkdir -p /tmp/.cortex \
             /tmp/obsidian-brain/cortex/memories \
             /tmp/obsidian-brain/cortex/playbooks

# Start the MCP server over stdio — this is the same entrypoint
# bootstrap.sh exec's in a real install.
CMD [".venv/bin/python", "-m", "cortex_server.server"]
