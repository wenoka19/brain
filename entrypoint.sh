#!/usr/bin/env bash
# Container entrypoint: restore from object storage, then serve.
#
# Litestream owns the database files. On a cold start it restores the most
# recent replica before anything opens them, and then supervises the server so
# every write is streamed out continuously.
set -euo pipefail

DATA_DIR="${BRAIN_DATA_DIR:-/data}"
CONTROL_DB="${DATA_DIR}/control.db"
ROLE="${BRAIN_ROLE:-web}"

mkdir -p "${DATA_DIR}/users" "${DATA_DIR}/uploads"

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
