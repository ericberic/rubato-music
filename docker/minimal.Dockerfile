# syntax=docker/dockerfile:1

# ---- Builder stage ----
FROM python:3.11-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=.venv \
    PATH="/root/.local/bin:$PATH"

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    git \
    libgl1 \
    && rm -rf /var/lib/apt/lists/*

RUN python -m pip install --no-cache-dir --upgrade pip uv

WORKDIR /workspace

COPY . .
RUN uv sync --extra dev --frozen \
    && uv pip install pytest pytest-cov \
    && uv pip install --no-deps .

# ---- Runtime stage ----
FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/home/rubato/workspace/.venv/bin:$PATH"

RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 \
    libglib2.0-0 \
    libnspr4 \
    libnss3 \
    libdbus-1-3 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libxkbcommon0 \
    libatspi2.0-0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libcairo2 \
    libpango-1.0-0 \
    libasound2 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --shell /bin/bash rubato

WORKDIR /home/rubato/workspace

COPY --from=builder --chown=rubato:rubato /workspace /home/rubato/workspace

RUN PLAYWRIGHT_BROWSERS_PATH=/home/rubato/.cache/ms-playwright \
    /home/rubato/workspace/.venv/bin/python -m playwright install chromium \
    && chown -R rubato:rubato /home/rubato/.cache/ms-playwright

USER rubato

CMD ["python","-m","pytest"]
