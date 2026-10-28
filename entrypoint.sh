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

# `app.cli serve` passes uvicorn --proxy-headers --forwarded-allow-ips=* itself,
# so the flags live in one place rather than in three launchers that can drift.

# `snapshots` holds the `VACUUM INTO` staging file on its way to R2.
mkdir -p "${DATA_DIR}/users" "${DATA_DIR}/uploads" "${DATA_DIR}/snapshots"

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
