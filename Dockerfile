# syntax=docker/dockerfile:1

# ---- build the wheels in a throwaway stage -------------------------------
FROM python:3.12-slim AS build
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
WORKDIR /build
RUN apt-get update && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml ./

# `pyproject.toml` is the single source of truth: this stage READS the dependency
# list rather than restating it. The restated copy that used to live here was the
# only place `mcp` and `yt-dlp` were declared, and it asked for `mcp>=1.2` while
# the code imports 2.x-only module paths — a build that resolved 1.x would go
# green and then fail at import.
#
# The project itself is deliberately NOT installed: the runtime stage runs it from
# `/app`, and a second copy in site-packages would shadow it for any entry point
# whose `sys.path[0]` is not the working directory.
#
# Only `pyproject.toml` is copied, so this layer still caches across application
# changes.
RUN python3 <<'PYDEPS'
import tomllib

with open("pyproject.toml", "rb") as handle:
    deps = tomllib.load(handle)["project"]["dependencies"]

if not deps:
    raise SystemExit("pyproject.toml declares no dependencies")

with open("/tmp/requirements.txt", "w", encoding="utf-8") as out:
    out.write("\n".join(deps) + "\n")

print(f"resolved {len(deps)} dependencies from pyproject.toml")
PYDEPS

RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install -r /tmp/requirements.txt

# ---- runtime -------------------------------------------------------------
FROM python:3.12-slim AS runtime
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:${PATH}" \
    BRAIN_ENV=prod \
    BRAIN_DATA_DIR=/data \
    PORT=8080

# ca-certificates for outbound HTTPS; litestream for replication; deno because
# yt-dlp needs a JavaScript runtime to read YouTube.
#
# That last one is not optional any more. Without it yt-dlp logs "No supported
# JavaScript runtime could be found ... YouTube extraction without a JS runtime
# has been deprecated" and then "No title found in player response", and every
# single video fails. Measured on production: three of the most public videos
# on YouTube, all refused, with nothing in the image to explain it. `deno` is
# the one runtime yt-dlp enables by default.
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl unzip \
    && curl -sSL https://github.com/benbjohnson/litestream/releases/download/v0.3.13/litestream-v0.3.13-linux-amd64.tar.gz \
       | tar -xz -C /usr/local/bin litestream \
    && curl -sSL https://github.com/denoland/deno/releases/download/v2.1.4/deno-x86_64-unknown-linux-gnu.zip -o /tmp/deno.zip \
    && unzip -q /tmp/deno.zip -d /usr/local/bin && chmod +x /usr/local/bin/deno && rm /tmp/deno.zip \
    && apt-get purge -y curl unzip && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

COPY --from=build /opt/venv /opt/venv
WORKDIR /app
COPY app ./app
COPY litestream.yml /etc/litestream.yml
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# The app runs as uid 10001. There is deliberately no `USER` instruction: a
# platform mount (Railway's volume, Fly's [mounts]) is attached at run time and
# shadows this build-time `chown`, arriving owned by root — so the container has
# to start as root, take ownership of the mount, and only then drop privileges.
# `entrypoint.sh` does exactly that and re-execs itself under uid 10001, so no
# long-running process here is root. `docker run --user 10001` also works: the
# entrypoint detects that it is already unprivileged and skips the drop.
RUN useradd --system --create-home --uid 10001 brainuser \
    && mkdir -p /data/users /data/uploads /data/snapshots \
    && chown -R brainuser:brainuser /data /app

# `VOLUME ["/data"]` is deliberately NOT declared. Railway rejects a build that
# contains it: there, the volume is attached to the service in the dashboard and
# mounted at /data at run time, so a Dockerfile-declared anonymous volume both
# conflicts with that and would hide the mount. Fly and plain `docker run` get
# the same path from their own mount flags (`fly.toml` [mounts], `-v`), so
# nothing else needs this line either.
#
# What the line did *not* do is create the directory — that is the `mkdir` above
# and in `entrypoint.sh`, both of which still run.

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,os,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8080')+'/healthz',timeout=4).status==200 else 1)"

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
