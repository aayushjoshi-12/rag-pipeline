FROM python:3.15-slim

WORKDIR /app

# `upgrade` picks up the Debian security fixes published since the base image was built (the release gate scans the result).
RUN apt-get update && apt-get upgrade -y && apt-get install -y --no-install-recommends \
    curl \
    tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.12.19 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_HTTP_TIMEOUT=300 UV_HTTP_RETRIES=5

# Dependencies first (cached layer), then the project itself.
COPY pyproject.toml uv.lock* README.md ./
# The cache mount keeps uv's download cache out of the image layer (it was ~600 MB) and speeds rebuilds.
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-install-project

COPY app/ ./app/
COPY config/ ./config/
COPY alembic/ ./alembic/
COPY alembic.ini ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH"

# Run as a non-root user; /app/data holds uploads (mount a volume there).
RUN useradd --create-home --uid 10001 app \
    && mkdir -p /app/data \
    && chown -R app:app /app/data /home/app
USER 10001
ENV HOME=/home/app

EXPOSE 8000

# Run the venv binaries directly: `uv run` would re-resolve/sync dependencies at container start.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
