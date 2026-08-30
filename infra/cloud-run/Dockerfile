# syntax=docker/dockerfile:1
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/src

WORKDIR /app

# System deps kept minimal; lxml needs libxml2/libxslt at runtime (slim includes them).
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
  && rm -rf /var/lib/apt/lists/*

# Copy dependency manifests first for layer caching.
COPY pyproject.toml README.md ./
COPY src/ ./src/

RUN pip install --upgrade pip \
  && pip install --no-cache-dir . \
  && pip install --no-cache-dir "uvicorn[standard]>=0.29.0" "fastapi>=0.110.0"

# Non-root user for least privilege.
RUN useradd -m -u 10001 appuser \
  && chown -R appuser:appuser /app
USER appuser

EXPOSE 8080

# Health and retrieval routes are mounted at /health, /entries, /search/semantic, etc.
# PORT is injected by Cloud Run; default 8080 for local `docker run -p 8080:8080`.
CMD ["sh", "-c", "uvicorn aksantara.api.routes:create_app --factory --host 0.0.0.0 --port ${PORT:-8080}"]
