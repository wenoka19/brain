#!/usr/bin/env bash
# Idempotent Cloud Session VM setup for `brain`.
# Safe to re-run: pip install is a no-op when the wheel cache is warm.
set -euo pipefail

cd "$(dirname "$0")/.."

echo "[brain/setup] python: $(python3 --version)"

# `pyproject.toml` is the single source of truth: read the dependency list,
# never restate it here. The copy that used to live in this file had already
# drifted from the image's — it was missing `mcp` and `yt-dlp` entirely, so a
# fresh Cloud VM could not run the MCP tests and `cli doctor` warned about
# YouTube ingestion.
mapfile -t PKGS < <(python3 - <<'PYDEPS'
import tomllib

with open("pyproject.toml", "rb") as handle:
    project = tomllib.load(handle)["project"]

for spec in project["dependencies"]:
    print(spec)
for spec in project.get("optional-dependencies", {}).get("dev", []):
    print(spec)
PYDEPS
)

test "${#PKGS[@]}" -gt 0 || { echo "[brain/setup] read no dependencies from pyproject.toml" >&2; exit 1; }
echo "[brain/setup] installing ${#PKGS[@]} declared dependencies"
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

# `mcp` is imported by `app/main.py` and `app/mcp_server.py`, and this script
# used to omit it, so the MCP tests failed here for a reason that looked like a
# broken test rather than a broken setup.
from mcp.server.mcpserver import MCPServer  # noqa: F401

print("[brain/setup] sqlite-vec + FTS5 + mcp OK")
PYCHECK

# `yt-dlp` is run as a PATH executable, not imported, so an installed-but-not-on-PATH
# wheel would still break YouTube ingestion. Say so here rather than at ingest time.
#
# And being on PATH is not the question. Production had yt-dlp on PATH, deno on
# PATH, and `deno --version` exiting 0, and could not read a single video: the
# deno it carried was below yt-dlp's minimum, so yt-dlp refused it and fell back
# to its JS-less clients. `youtube-check` asks yt-dlp itself, offline.
if BRAIN_ENV=prod python3 -m app.cli youtube-check; then
  :
else
  echo "[brain/setup] WARNING: YouTube ingestion will fail on this host (see above)"
fi

mkdir -p .brain_data/users
echo "[brain/setup] done"
