# Base layer for Rubato CI builds. Contains Python 3.11 with all system
# dependencies required to install project wheels and run tests.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    PATH="/root/.local/bin:$PATH"

# Install system dependencies used to build native wheels (torch, numpy, etc.)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    git \
    pkg-config \
    libgl1 \
    && rm -rf /var/lib/apt/lists/*

# Install uv once so downstream Dockerfiles don't need to download it again.
RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir uv

WORKDIR /app
