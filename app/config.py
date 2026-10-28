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
    # `python -m app.cli calibrate` measures these against a real multi-topic
    # library. It confirmed hash_search_floor at 0.183 against the 0.18 shipped
    # here. The link floors are a judgement call rather than a statistic — see
    # that command's output for why — and 0.15 is the value that admits the
    # genuine outbound contradictions while excluding a measured false positive
    # between two unrelated topics.
    #
    # Both pairs are overridable, because a different neural model may need
    # recalibrating again — and the shipped default now does. The neural pair
    # was reasoned for a model that returns 256 dimensions natively; the
    # default `qwen/qwen3-embedding-8b` returns 4096 and is cut to a 256-float
    # Matryoshka prefix, which moves the whole similarity distribution. Run
    # `python -m app.cli calibrate` once with a live key before trusting them.
    hash_search_floor: float = 0.18
    hash_link_floor: float = 0.15
    neural_search_floor: float = 0.42
    neural_link_floor: float = 0.38
    search_floor_override: float | None = None
    link_floor_override: float | None = None

    # ---- live AI providers ------------------------------------------------
    # One key, one host, three jobs. OpenRouter serves chat, vision and
    # `/embeddings`, so `LLM_API_KEY` alone turns the whole live stack on and
    # `EMBEDDING_*` exists only for the operator who wants a second host.
    llm_base_url: str = "https://openrouter.ai/api/v1"
    llm_api_key: str = ""
    #: Native vision, a very large context, and cheap enough to extract every
    #: chunk of a two-hour transcript. `deepseek/deepseek-v4.1-flash` and
    #: `qwen/qwen3.8-flash` are drop-in alternatives via `LLM_MODEL`.
    llm_model: str = "z-ai/glm-5.3-flash"
    #: This class of model reasons before answering. Extraction is a structured
    #: transcription task, not a puzzle, so the lowest setting keeps latency and
    #: output tokens down; `""` omits the field for hosts that reject it.
    llm_reasoning_effort: str = "low"
    llm_timeout_sec: float = 60.0
    llm_max_retries: int = 2
    llm_max_output_tokens: int = 4096
    embedding_base_url: str = ""
    embedding_api_key: str = ""
    #: Matryoshka-trained, so a 256-float prefix of its native vector is itself
    #: a valid embedding — see `LiveEmbedder._normalise`.
    embedding_model_name: str = "qwen/qwen3-embedding-8b"
    embedding_timeout_sec: float = 30.0
    vision_max_bytes: int = 8 * MB

    # ---- portability: tenant snapshots + object storage -------------------
    # Tenant databases are replicated as whole-file snapshots rather than by
    # Litestream (see app/storage/r2_sync.py for why), so these govern how
    # often a dirty tenant is vacuumed out and pushed to R2.
    r2_timeout_sec: float = 30.0
    snapshot_dir: Path = Path(".brain_data/snapshots")
    snapshot_debounce_sec: float = 2.0
    snapshot_poll_sec: float = 1.0
    #: 0 = keep every tenant file on local disk. Above 0, the least recently
    #: used *already-synced* tenants are evicted past that many files.
    max_local_tenants: int = 0

    # ---- portability: horizontal sharding ---------------------------------
    #: A node serves the tenants that hash to its index. 1/0 = single node,
    #: which is Stage 1 and every local run.
    shard_total: int = 1
    shard_index: int = 0
    #: Index-aligned addresses for the other shards. Either is enough:
    #: `shard_urls` routes with a 307, `shard_instances` with `fly-replay`.
    shard_urls: tuple[str, ...] = ()
    shard_instances: tuple[str, ...] = ()
    #: Fallback when a shard has no explicit entry above: Fly's `fly-replay`
    #: takes a machine name, so a deployment that names its machines by index
    #: needs no table at all. Set `BRAIN_SHARD_INSTANCE_TEMPLATE=""` to turn the
    #: convention off and require an explicit address.
    shard_instance_template: str = "shard-{index}"

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
    def targets_openrouter(self) -> bool:
        return "openrouter.ai" in self.llm_base_url

    @property
    def chat_extras(self) -> dict[str, object]:
        """Host-specific fields merged into every chat completions payload.

        `reasoning` is OpenRouter's own parameter. Sending it to a plain
        OpenAI-compatible host would be an unknown field, and some reject the
        whole request for one, so it is gated on the host rather than assumed.
        """
        if self.targets_openrouter and self.llm_reasoning_effort:
            return {"reasoning": {"effort": self.llm_reasoning_effort}}
        return {}

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
    def sharded(self) -> bool:
        return self.shard_total > 1

    def shard_for(self, user_id: str) -> int:
        """Which shard owns a tenant.

        crc32 is stable across processes, machines and Python versions, which
        is the whole point: every node must agree on the owner without asking
        anyone. (`hash()` is randomised per process and would not.)
        """
        from zlib import crc32

        if self.shard_total <= 1:
            return 0
        return crc32(user_id.encode("utf-8")) % self.shard_total

    def owns_shard(self, user_id: str) -> bool:
        return self.shard_for(user_id) == self.shard_index

    def shard_address(self, index: int) -> tuple[str, str]:
        """``(kind, value)`` for a shard: ``("instance", id)``,
        ``("url", base)``, or ``("", "")`` when it has no address.

        An explicit entry always wins, because a real Fly machine id is a hex
        string rather than anything guessable; the template is the convenience
        for a deployment that names its machines after their shard.
        """
        if 0 <= index < len(self.shard_instances):
            return "instance", self.shard_instances[index]
        if 0 <= index < len(self.shard_urls):
            return "url", self.shard_urls[index].rstrip("/")
        if self.shard_instance_template:
            return "instance", self.shard_instance_template.format(index=index)
        return "", ""

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
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)


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


def _list_env(name: str) -> tuple[str, ...]:
    raw = os.environ.get(name) or ""
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _int_env(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    try:
        return int(raw)
    except ValueError:
        return default


def load_settings() -> Settings:
    env = _env()
    defaults = Settings()
    data_dir = _data_dir(env)
    shard_total = max(1, _int_env("BRAIN_SHARD_TOTAL", defaults.shard_total))
    shard_index = _int_env("BRAIN_SHARD_INDEX", defaults.shard_index)
    if not 0 <= shard_index < shard_total:
        shard_index = 0
    return Settings(
        env=env,
        data_dir=data_dir,
        snapshot_dir=Path(
            os.environ.get("BRAIN_SNAPSHOT_DIR") or (data_dir / "snapshots")
        ).expanduser(),
        max_local_tenants=max(0, _int_env("MAX_LOCAL_TENANTS", defaults.max_local_tenants)),
        shard_total=shard_total,
        shard_index=shard_index,
        shard_urls=_list_env("BRAIN_SHARD_URLS"),
        shard_instances=_list_env("BRAIN_SHARD_INSTANCES"),
        shard_instance_template=(
            os.environ["BRAIN_SHARD_INSTANCE_TEMPLATE"].strip()
            if "BRAIN_SHARD_INSTANCE_TEMPLATE" in os.environ
            else defaults.shard_instance_template
        ),
        audience=os.environ.get("BRAIN_AUDIENCE", defaults.audience),
        llm_base_url=(os.environ.get("LLM_BASE_URL") or defaults.llm_base_url).rstrip("/"),
        llm_api_key=(os.environ.get("LLM_API_KEY") or "").strip(),
        llm_model=os.environ.get("LLM_MODEL") or defaults.llm_model,
        embedding_base_url=(os.environ.get("EMBEDDING_BASE_URL") or "").strip(),
        # Most hosts serve both endpoints off one key, so the LLM key is the
        # fallback rather than a second thing to configure.
        embedding_api_key=(os.environ.get("EMBEDDING_API_KEY") or "").strip(),
        embedding_model_name=os.environ.get("EMBEDDING_MODEL") or defaults.embedding_model_name,
        llm_reasoning_effort=(
            os.environ["LLM_REASONING_EFFORT"].strip()
            if "LLM_REASONING_EFFORT" in os.environ
            else defaults.llm_reasoning_effort
        ),
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
