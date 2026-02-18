# syntax=docker/dockerfile:1

FROM python:3.11-slim

# Environment hygiene
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    UVICORN_WORKERS=2

WORKDIR /app

# Minimal system deps (CA certs for outbound HTTPS in case future deps need it)
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# Install Python deps first to leverage Docker layer cache
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# Copy application code
COPY app ./app

# Create non-root user and writable data directory for SQLite when using a volume
RUN useradd -m -u 1001 appuser && mkdir -p /data && chown -R appuser:appuser /app /data
USER appuser

EXPOSE 8000

# HEALTHCHECK using Python stdlib (no curl/wget required)
HEALTHCHECK --interval=10s --timeout=3s --retries=5 CMD python -c "import sys,urllib.request; sys.exit(0) if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2).status==200 else sys.exit(1)"

# Default command (dev/prod can override via compose)
CMD [ "sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers ${UVICORN_WORKERS}" ]
