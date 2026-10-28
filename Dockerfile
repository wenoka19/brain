# syntax=docker/dockerfile:1

# ---- build the wheels in a throwaway stage -------------------------------
FROM python:3.12-slim AS build
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
WORKDIR /build
RUN apt-get update && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml ./
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install \
        "rapidfuzz>=3.9" "sqlite-vec>=0.1.6" "fastapi>=0.110" "uvicorn[standard]>=0.27" \
        "httpx>=0.27" "PyJWT>=2.8" "pypdf>=4.0" "python-docx>=1.1" \
        "beautifulsoup4>=4.12" "lxml>=5.0" "python-multipart>=0.0.9" "mcp>=1.2" \
        "yt-dlp>=2024.8.6"

# ---- runtime -------------------------------------------------------------
FROM python:3.12-slim AS runtime
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:${PATH}" \
    BRAIN_ENV=prod \
    BRAIN_DATA_DIR=/data \
    PORT=8080

# ca-certificates for outbound HTTPS; litestream for replication.
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
    && curl -sSL https://github.com/benbjohnson/litestream/releases/download/v0.3.13/litestream-v0.3.13-linux-amd64.tar.gz \
       | tar -xz -C /usr/local/bin litestream \
    && apt-get purge -y curl && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

COPY --from=build /opt/venv /opt/venv
WORKDIR /app
COPY app ./app
COPY litestream.yml /etc/litestream.yml
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# The data volume is owned by a non-root user; nothing here needs root.
RUN useradd --system --create-home --uid 10001 brain \
    && mkdir -p /data/users /data/uploads \
    && chown -R brain:brain /data /app
USER brain
VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,os,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8080')+'/healthz',timeout=4).status==200 else 1)"

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
