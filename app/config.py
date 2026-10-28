"""Central settings for `brain`.

`BRAIN_ENV` drives every behavioural fork in the codebase:

* ``test``  -> deterministic providers, no network, no subprocesses, data under
  a repo-local scratch directory (or ``BRAIN_DATA_DIR``).
* ``prod``  -> live providers (yt-dlp / HTTP / LLM), data under ``/data`` so
  Litestream can replicate it.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

BrainEnv = Literal["test", "prod"]

#: Tenant ids are used to build file paths, so they are strictly validated.
USER_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

MB = 1024 * 1024


@dataclass(frozen=True)
class Settings:
    env: BrainEnv = "test"
    data_dir: Path = Path(".brain_data")

    # ---- embeddings -------------------------------------------------------
    embedding_dim: int = 256
    embedding_model: str = "v1"
    extractor_version: str = "v1"

    # ---- hybrid search ----------------------------------------------------
    rrf_k: int = 60
    fts_candidates: int = 40
    vec_candidates: int = 40
    search_limit: int = 8

    # ---- grounding --------------------------------------------------------
    grounding_threshold: int = 88
    anchor_min_words: int = 10
    anchor_max_words: int = 40

    # ---- contradiction linker --------------------------------------------
    link_top_k: int = 8
    # Calibrated for the v1-hash embedder, where near-duplicate claims land
    # around 0.2-0.4 rather than 0.8+. The shape classifier is the real gate;
    # this floor only keeps the candidate set cheap.
    link_similarity_floor: float = 0.15

    # ---- anti-abuse -------------------------------------------------------
    tool_calls_per_min: int = 30
    ingestions_per_min: int = 10
    duplicate_call_window_sec: float = 15.0

    # ---- ingestion caps ---------------------------------------------------
    max_upload_bytes: int = 20 * MB
    max_docx_decompressed_bytes: int = 30 * MB
    max_zip_compression_ratio: int = 120
    max_pdf_pages: int = 200
    max_media_minutes: int = 120
    subprocess_timeout_sec: int = 45
    ytdlp_max_filesize: str = "25M"
    http_timeout_sec: float = 20.0
    max_redirect_hops: int = 5
    max_fetch_bytes: int = 20 * MB

    # ---- per-user quotas --------------------------------------------------
    quota_media_minutes: int = 300
    quota_files: int = 50

    # ---- auth -------------------------------------------------------------
    access_token_ttl_sec: int = 3600
    refresh_token_ttl_sec: int = 30 * 24 * 3600
    audience: str = "https://brain.local/mcp"

    allowed_file_exts: frozenset[str] = field(
        default_factory=lambda: frozenset(
            {"pdf", "docx", "txt", "md", "png", "jpg", "jpeg", "webp"}
        )
    )
    blocked_file_exts: frozenset[str] = field(
        default_factory=lambda: frozenset(
            {"mp4", "mov", "mkv", "avi", "webm", "mp3", "wav", "m4a", "flac", "aac"}
        )
    )

    @property
    def is_test(self) -> bool:
        return self.env == "test"

    @property
    def control_db_path(self) -> Path:
        return self.data_dir / "control.db"

    @property
    def users_dir(self) -> Path:
        return self.data_dir / "users"

    def tenant_db_path(self, user_id: str) -> Path:
        """Resolve a tenant database path from a *verified* user id.

        The caller must have read ``user_id`` out of ``control.db``; it is never
        taken from client input. The regex check is a second line of defence
        against traversal (``../``) and absolute paths.
        """
        if not USER_ID_RE.match(user_id or ""):
            raise ValueError(f"unsafe user_id: {user_id!r}")
        return self.users_dir / f"user_{user_id}.db"

    def ensure_dirs(self) -> None:
        self.users_dir.mkdir(parents=True, exist_ok=True)


def _env() -> BrainEnv:
    raw = (os.environ.get("BRAIN_ENV") or "test").strip().lower()
    return "prod" if raw == "prod" else "test"


def _data_dir(env: BrainEnv) -> Path:
    override = os.environ.get("BRAIN_DATA_DIR")
    if override:
        return Path(override).expanduser()
    return Path("/data") if env == "prod" else Path(".brain_data").resolve()


def load_settings() -> Settings:
    env = _env()
    return Settings(
        env=env,
        data_dir=_data_dir(env),
        audience=os.environ.get("BRAIN_AUDIENCE", Settings.audience),
    )


_settings: Settings | None = None


def settings() -> Settings:
    """Process-wide settings singleton (re-read with :func:`reload_settings`)."""
    global _settings
    if _settings is None:
        _settings = load_settings()
    return _settings


def reload_settings() -> Settings:
    """Re-read the environment. Used by tests that relocate ``BRAIN_DATA_DIR``."""
    global _settings
    _settings = load_settings()
    return _settings
