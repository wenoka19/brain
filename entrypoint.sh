#!/usr/bin/env bash
# Container entrypoint: restore from object storage, then serve.
#
# Litestream owns `control.db` only. On a cold start it restores the most recent
# replica before anything opens it, then supervises the server so every write
# is streamed out continuously. Tenant databases are not Litestream's — they
# hydrate from R2 on first open and snapshot after a write (app/storage/
# r2_sync.py), so a cold node needs no restore step for them.
set -euo pipefail

DATA_DIR="${BRAIN_DATA_DIR:-/data}"
CONTROL_DB="${DATA_DIR}/control.db"
ROLE="${BRAIN_ROLE:-web}"
APP_UID=10001
APP_GID=10001

# ---------------------------------------------------------------------------
# Take ownership of the mount, then stop being root.
#
# A platform mount is attached at run time and shadows whatever the image did
# to that path at build time — so the `chown` in the Dockerfile is simply not
# there any more, and the mount arrives owned by root. The container therefore
# starts as root for exactly as long as it takes to fix that, then drops to the
# unprivileged user and `exec`s itself, so the server, the worker and Litestream
# all run as uid 10001 with no capabilities to lose.
#
# Running the whole container as root would be the one-line alternative, and is
# what most deploy guides suggest. It is not worth it: this process parses
# untrusted PDFs, DOCX archives and remote HTML.
# ---------------------------------------------------------------------------
if [[ "$(id -u)" == "0" ]]; then
  mkdir -p "${DATA_DIR}/users" "${DATA_DIR}/uploads" "${DATA_DIR}/snapshots"
  chown -R "${APP_UID}:${APP_GID}" "${DATA_DIR}" || \
    echo "[brain] could not chown ${DATA_DIR}; continuing as root"
  if command -v setpriv >/dev/null 2>&1; then
    echo "[brain] dropping to uid ${APP_UID} (setpriv)"
    exec setpriv --reuid="${APP_UID}" --regid="${APP_GID}" --init-groups "$0" "$@"
  elif command -v su >/dev/null 2>&1 && id -u brainuser >/dev/null 2>&1; then
    echo "[brain] dropping to brainuser (su)"
    exec su -s /bin/bash -c "$(printf '%q ' "$0" "$@")" brainuser
  fi
  # Neither tool is present: say so out loud rather than quietly running the
  # ingestion pipeline with root privileges.
  echo "[brain] WARNING: no setpriv/su available — running as root"
fi

# Already unprivileged (a `docker run --user`, or the re-exec above). Repeated
# because this is also the path taken when the container was never root, and it
# has to fail here rather than three layers deeper inside SQLite.
#
# A write canary, not just `mkdir -p`: on a directory that already exists mkdir
# succeeds whatever its permissions, so it would have reported success on a
# read-only mount and left the first `PRAGMA journal_mode = WAL` to fail.
mkdir -p "${DATA_DIR}/users" "${DATA_DIR}/uploads" "${DATA_DIR}/snapshots" 2>/dev/null || true
if ! ( : > "${DATA_DIR}/.writable" ) 2>/dev/null; then
  echo "[brain] FATAL: ${DATA_DIR} is not writable by uid $(id -u)." >&2
  echo "[brain] Owner is: $(stat -c '%U:%G %a' "${DATA_DIR}" 2>/dev/null || echo unknown)" >&2
  echo "[brain] On Railway, attach a volume to this service with mount path" >&2
  echo "[brain] ${DATA_DIR} — the entrypoint takes ownership of it at boot," >&2
  echo "[brain] which needs the container to start as root (no USER in the" >&2
  echo "[brain] Dockerfile). SQLite cannot run on a read-only directory." >&2
  exit 1
fi
rm -f "${DATA_DIR}/.writable"
for required in users uploads snapshots; do
  if [[ ! -d "${DATA_DIR}/${required}" ]]; then
    echo "[brain] FATAL: ${DATA_DIR}/${required} is missing and could not be created." >&2
    exit 1
  fi
done

# `app.cli serve` passes uvicorn --proxy-headers --forwarded-allow-ips=* itself,
# so the flags live in one place rather than in three launchers that can drift.

if [[ "${ROLE}" == "web" && -n "${LITESTREAM_REPLICA_URL:-}" ]]; then
  if [[ ! -f "${CONTROL_DB}" ]]; then
    echo "[brain] no local control.db; restoring from ${LITESTREAM_REPLICA_URL%%\?*}"
    litestream restore -if-replica-exists -config /etc/litestream.yml "${CONTROL_DB}" || \
      echo "[brain] no replica yet — starting from an empty database"
  fi
  echo "[brain] doctor"
  python -m app.cli doctor || echo "[brain] doctor reported problems; continuing"
  echo "[brain] starting web under litestream"
  exec litestream replicate -config /etc/litestream.yml -exec \
    "python -m app.cli serve --host 0.0.0.0 --port ${PORT:-8080}"
fi

if [[ "${ROLE}" == "worker" ]]; then
  # The worker shares the volume with the web process, which owns replication.
  echo "[brain] starting ingestion worker"
  exec python -m app.cli worker
fi

echo "[brain] starting web without replication (LITESTREAM_REPLICA_URL unset)"
python -m app.cli doctor || true
exec python -m app.cli serve --host 0.0.0.0 --port "${PORT:-8080}"
