# syntax=docker/dockerfile:1.7

############################
# Stage 1: builder
############################
FROM python:3.11-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Build deps for native wheels (asyncpg, bcrypt, cryptography, paramiko)
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        gcc \
        libffi-dev \
        libssl-dev \
        libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Copy project metadata first to leverage layer caching
COPY pyproject.toml README.md ./

# Create venv we can copy into the runtime stage
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

RUN pip install --upgrade pip setuptools wheel

# Copy source so editable install resolves the package
COPY mgmt_ui ./mgmt_ui

# Install the project (editable so the venv has all deps resolved)
RUN pip install -e .

############################
# Stage 2: runtime
############################
FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

# Runtime libs only (no compilers)
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
        libffi8 \
        libssl3 \
        openssh-client \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Non-root user
RUN groupadd --system app && \
    useradd --system --gid app --create-home --home-dir /home/app --shell /bin/bash app

WORKDIR /app

# Bring over the venv and source from the builder
COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /build /app

RUN chown -R app:app /app /home/app

USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/health || exit 1

CMD ["uvicorn", "mgmt_ui.app.main:app", "--host", "0.0.0.0", "--port", "8000"]
