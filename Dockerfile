# syntax=docker/dockerfile:1

FROM ghcr.io/ericberic/rubato-ci-base:py311 AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    PATH="/root/.local/bin:$PATH"

WORKDIR /app

# Dependency metadata first for better caching
COPY pyproject.toml uv.lock ./
RUN uv sync --extra dev --frozen --no-install-project

# Copy source + install project deps
COPY . .
RUN uv pip install --no-deps .

FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    PATH="/home/rubato/.local/bin:$PATH"

# Runtime-only packages + non-root user
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    libgl1 \
    && useradd --create-home --shell /bin/bash rubato \
    && rm -rf /var/lib/apt/lists/*

USER rubato
WORKDIR /app

# Install uv for the runtime user (adds to ~/.local/bin)
RUN python -m pip install --user --no-cache-dir uv

# Bring in the synced environment + source from the builder stage
COPY --from=builder --chown=rubato:rubato /app /app

# Run the pytest suite inside the uv-managed environment
CMD ["uv", "run", "pytest"]
