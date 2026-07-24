FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install uv for faster dependency management
RUN pip install --no-cache-dir uv

COPY . .

# Install Python dependencies using uv sync
RUN uv sync --frozen --no-dev --extra disk

# AW native parity: add the Firestore key-value backend so OAuth proxy sessions
# and the re-auth policy can persist across Cloud Run cold starts. Installed
# on top of the frozen sync (like the upstream Valkey extra, which is also not
# in the default image) so the base lock file is untouched.
RUN uv pip install "py-key-value-aio[firestore]"

# Create non-root user for security
RUN useradd --create-home --shell /bin/bash app \
    && chown -R app:app /app

# Give read and write access to the store_creds volume
RUN mkdir -p /app/store_creds \
    && chown -R app:app /app/store_creds \
    && chmod 755 /app/store_creds

USER app

# Expose port (use default of 8000 if PORT not set)
EXPOSE 8000
# Expose additional port if PORT environment variable is set to a different value
ARG PORT
EXPOSE ${PORT:-8000}

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD sh -c 'curl -f http://localhost:${PORT:-8000}/health || exit 1'

# Set environment variables for Python startup args
ENV TOOL_TIER=""
ENV TOOLS=""

# Use entrypoint for the base command and CMD for args.
# --no-sync: run against the already-built environment WITHOUT re-syncing, so the
# supplemental Firestore extra installed above (not in the frozen lock) is not
# stripped at container start. Without this, `uv run` re-syncs to the exact lock
# and Firestore silently disappears, reintroducing cold-start session loss.
ENTRYPOINT ["/bin/sh", "-c"]
CMD ["uv run --no-sync main.py --transport streamable-http ${TOOL_TIER:+--tool-tier \"$TOOL_TIER\"} ${TOOLS:+--tools $TOOLS}"]
