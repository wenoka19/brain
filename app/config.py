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

    # ---- retrieval floors, calibrated per embedding provider -------------
    # Cosine KNN always returns its k nearest rows, however far away they are,
    # so without a floor `search` answers a nonsense query with the library's
    # least-unrelated claims — junk the host LLM might then cite.
    #
    # The right floor depends entirely on which embedder is active, because the
    # two live on different scales:
    #
    #   deterministic_v1 (hashed bag of words) — on-topic queries measure 0.29+,
    #     off-topic ones top out near 0.14 on hash collisions alone.
    #   neural (a trained dense model) — unrelated text sits far higher, around
    #     0.2-0.3, so the same floors would admit noise.
    #
    # Both pairs are overridable, because a different neural model may need
    # recalibrating again.
    hash_search_floor: float = 0.18
    hash_link_floor: float = 0.15
    neural_search_floor: float = 0.42
    neural_link_floor: float = 0.38
    search_floor_override: float | None = None
    link_floor_override: float | None = None

    # ---- live AI providers ------------------------------------------------
    llm_base_url: str = "https://openrouter.ai/api/v1"
    llm_api_key: str = ""
    llm_model: str = "google/gemini-2.5-flash"
    llm_timeout_sec: float = 60.0
    llm_max_retries: int = 2
    llm_max_output_tokens: int = 4096
    embedding_base_url: str = ""
    embedding_api_key: str = ""
    embedding_model_name: str = "text-embedding-3-small"
    embedding_timeout_sec: float = 30.0
    vision_max_bytes: int = 8 * MB

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

    # ---- which providers are actually live -------------------------------
    @property
    def live_llm(self) -> bool:
        """True when a real extraction / classification model is configured."""
        return self.env == "prod" and bool(self.llm_api_key)

    @property
    def live_embeddings(self) -> bool:
        return self.env == "prod" and bool(self.embedding_api_key or self.llm_api_key)

    @property
    def live_vision(self) -> bool:
        return self.live_llm

    @property
    def embedding_provider(self) -> str:
        """The id recorded on every claim, and the key the floors are chosen by."""
        return self.embedding_model_name if self.live_embeddings else "deterministic_v1"

    @property
    def embedding_model(self) -> str:
        # Stored per claim, so a later re-embedding knows what produced a vector.
        return self.embedding_provider

    @property
    def extractor_version(self) -> str:
        # Stored per claim: which extractor proposed it, and therefore how much
        # the non-grounded fields are worth.
        return f"llm:{self.llm_model}" if self.live_llm else "heuristic-v1"

    @property
    def resolved_embedding_base_url(self) -> str:
        return (self.embedding_base_url or self.llm_base_url).rstrip("/")

    @property
    def resolved_embedding_api_key(self) -> str:
        return self.embedding_api_key or self.llm_api_key

    @property
    def search_similarity_floor(self) -> float:
        if self.search_floor_override is not None:
            return self.search_floor_override
        return self.neural_search_floor if self.live_embeddings else self.hash_search_floor

    @property
    def link_similarity_floor(self) -> float:
        if self.link_floor_override is not None:
            return self.link_floor_override
        return self.neural_link_floor if self.live_embeddings else self.hash_link_floor

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


def _float_env(name: str) -> float | None:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def load_settings() -> Settings:
    env = _env()
    defaults = Settings()
    return Settings(
        env=env,
        data_dir=_data_dir(env),
        audience=os.environ.get("BRAIN_AUDIENCE", defaults.audience),
        llm_base_url=(os.environ.get("LLM_BASE_URL") or defaults.llm_base_url).rstrip("/"),
        llm_api_key=(os.environ.get("LLM_API_KEY") or "").strip(),
        llm_model=os.environ.get("LLM_MODEL") or defaults.llm_model,
        embedding_base_url=(os.environ.get("EMBEDDING_BASE_URL") or "").strip(),
        # Most hosts serve both endpoints off one key, so the LLM key is the
        # fallback rather than a second thing to configure.
        embedding_api_key=(os.environ.get("EMBEDDING_API_KEY") or "").strip(),
        embedding_model_name=os.environ.get("EMBEDDING_MODEL") or defaults.embedding_model_name,
        search_floor_override=_float_env("BRAIN_SEARCH_FLOOR"),
        link_floor_override=_float_env("BRAIN_LINK_FLOOR"),
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
