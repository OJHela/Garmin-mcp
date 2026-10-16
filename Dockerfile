# Use Python 3.12 slim image for smaller size
FROM python:3.12-slim

# Set working directory
WORKDIR /app

# Install uv for faster dependency management
# https://github.com/astral-sh/uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_SYSTEM_PYTHON=1

# Copy dependency files and README first for better layer caching
COPY pyproject.toml README.md ./

# Copy the application source code (needed for editable install)
COPY src/ ./src/

# Install dependencies using uv
RUN uv pip install -e .

# Copy test files (optional, for testing in container)
COPY tests/ ./tests/
COPY pytest.ini ./

# Create directory for Garmin tokens
RUN mkdir -p /root/.garminconnect && \
    chmod 700 /root/.garminconnect

# Serve over HTTP by default (Railway). The port comes from $PORT, which
# Railway injects; GARMIN_MCP_PORT overrides it. Set GARMIN_MCP_TRANSPORT=stdio
# to get the original stdio behaviour (Claude Desktop, Inspector).
ENV GARMIN_MCP_TRANSPORT=streamable-http \
    GARMIN_MCP_HOST=0.0.0.0 \
    GARMIN_MCP_STATELESS_HTTP=true
EXPOSE 8000

# Set the entrypoint to run the MCP server
ENTRYPOINT ["garmin-mcp"]

# Health check (optional - adjust based on your needs)
# HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
#   CMD python -c "import sys; sys.exit(0)"
