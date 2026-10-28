#!/usr/bin/env bash
# Idempotent Cloud Session VM setup for `brain`.
# Safe to re-run: pip install is a no-op when the wheel cache is warm.
set -euo pipefail

cd "$(dirname "$0")/.."

echo "[brain/setup] python: $(python3 --version)"

PKGS=(
  "rapidfuzz>=3.9"
  "sqlite-vec>=0.1.6"
  "pytest>=8.0"
  "pytest-asyncio>=0.23"
  "fastapi>=0.110"
  "uvicorn>=0.27"
  "httpx>=0.27"
  "PyJWT>=2.8"
  "pypdf>=4.0"
  "python-docx>=1.1"
  "beautifulsoup4>=4.12"
  "lxml>=5.0"
  "python-multipart>=0.0.9"
)

python3 -m pip install --quiet --disable-pip-version-check "${PKGS[@]}"

# Verify the two native capabilities the whole stack depends on.
python3 - <<'PYCHECK'
import sqlite3, sqlite_vec
db = sqlite3.connect(":memory:")
db.enable_load_extension(True)
sqlite_vec.load(db)
db.enable_load_extension(False)
assert db.execute("select vec_version()").fetchone()
assert db.execute(
    "select 1 from pragma_compile_options where compile_options like '%FTS5%'"
).fetchone(), "SQLite build is missing FTS5"
print("[brain/setup] sqlite-vec + FTS5 OK")
PYCHECK

mkdir -p .brain_data/users
echo "[brain/setup] done"
