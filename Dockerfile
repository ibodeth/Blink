# syntax=docker/dockerfile:1

# ---------------------------------------------------------------------------
# Stage 1: frontend build (Node) -> static assets in /frontend/dist
# ---------------------------------------------------------------------------
FROM node:20-slim AS frontend-build
WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install
COPY frontend/ ./
RUN npm run build

# ---------------------------------------------------------------------------
# Stage 2: python dependency build -> wheels installed into a venv
# ---------------------------------------------------------------------------
FROM python:3.10-slim AS builder
ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY requirements.txt ./
# Headless image does not need pywebview (no display server).
RUN grep -v '^pywebview' requirements.txt > requirements.headless.txt \
    && pip install --no-cache-dir -r requirements.headless.txt

# ---------------------------------------------------------------------------
# Stage 3: production runtime (slim, non-root)
# ---------------------------------------------------------------------------
FROM python:3.10-slim AS runner
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    BLINK_HEADLESS=1 \
    BLINK_HOST=0.0.0.0 \
    BLINK_PORT=8000

# Runtime libraries for sounddevice / ffmpeg-based audio.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libportaudio2 libsndfile1 ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Create an unprivileged user.
RUN useradd --create-home --uid 10001 blink
WORKDIR /app

# Copy the prepared virtualenv and application code.
COPY --from=builder /opt/venv /opt/venv
COPY --chown=blink:blink . /app
COPY --from=frontend-build --chown=blink:blink /frontend/dist /app/frontend/dist

# Writable runtime directories.
RUN mkdir -p /app/logs /app/models \
    && chown -R blink:blink /app/logs /app/models

USER blink

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health').status==200 else 1)"

CMD ["python", "main.py", "--headless"]
