"""The front door: URL normalisation, V1 scope enforcement, file-bomb defence.

Nothing reaches a fetcher without passing through here. The gatekeeper is
deliberately *paranoid and cheap* — it runs before any network or subprocess
work, so a rejected input costs nothing.
"""

from __future__ import annotations

import hashlib
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from app.config import Settings, settings
from app.security.ssrf import SSRFBlocked, validate_url

SOURCE_TYPES = ("youtube", "article", "github", "pdf", "docx", "text", "image", "user_note")

#: Query parameters that only ever carry tracking, never identity.
TRACKING_PARAMS = frozenset(
    {
        "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "utm_id",
        "fbclid", "gclid", "msclkid", "igshid", "mc_cid", "mc_eid",
        "si", "feature", "app", "ref", "ref_src", "spm", "_hsenc", "_hsmi",
    }
)

YOUTUBE_HOSTS = frozenset(
    {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}
)
GITHUB_HOSTS = frozenset({"github.com", "www.github.com", "raw.githubusercontent.com", "gist.github.com"})

#: `R-23`: the three platforms besides YouTube whose video the founder wants
#: read. They are named here for one reason that has nothing to do with
#: fetching them yet: every host this file does not recognise falls through to
#: the generic-article branch below, and `LiveFetcher.fetch_url` sends
#: anything that is not `youtube` or `github` to `fetch_article`. So a TikTok
#: link was downloaded as an HTML page and handed to BeautifulSoup, which on a
#: JavaScript shell yields nothing — and the person was told "Nothing readable
#: was there", a sentence about their link rather than about the gap in this
#: product. Recognising the host is what makes the refusal honest.
TIKTOK_HOSTS = frozenset(
    {"tiktok.com", "www.tiktok.com", "m.tiktok.com", "vm.tiktok.com", "vt.tiktok.com"}
)
INSTAGRAM_HOSTS = frozenset({"instagram.com", "www.instagram.com", "m.instagram.com"})
FACEBOOK_HOSTS = frozenset(
    {"facebook.com", "www.facebook.com", "m.facebook.com", "web.facebook.com",
     "fb.watch", "fb.com", "www.fb.com"}
)
#: host set -> the name the person reads in the refusal.
PLATFORM_HOSTS: tuple[tuple[frozenset[str], str], ...] = (
    (TIKTOK_HOSTS, "TikTok"),
    (INSTAGRAM_HOSTS, "Instagram"),
    (FACEBOOK_HOSTS, "Facebook"),
)

_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_MEDIA_EXT_RE = re.compile(r"\.(mp4|mov|mkv|avi|webm|mp3|wav|m4a|flac|aac|ogg)(?:$|[?#])", re.I)

#: File magic prefixes, so an attacker cannot rename `payload.exe` to `.pdf`.
MAGIC = {
    "pdf": (b"%PDF-",),
    "docx": (b"PK\x03\x04",),
    "png": (b"\x89PNG\r\n\x1a\n",),
    "jpg": (b"\xff\xd8\xff",),
    "jpeg": (b"\xff\xd8\xff",),
    "webp": (b"RIFF",),
}
TEXT_EXTS = frozenset({"txt", "md"})
IMAGE_EXTS = frozenset({"png", "jpg", "jpeg", "webp"})


class GatekeeperError(Exception):
    """A rejection with a machine-readable reason and a human explanation."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


@dataclass(frozen=True)
class NormalizedTarget:
    source_type: str
    canonical_url: str
    original_url: str
    identifier: str = ""  # YouTube video id, GitHub path, ...
    estimated_minutes: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# URL normalisation
# ---------------------------------------------------------------------------
def _strip_tracking(query: str) -> str:
    kept = [(k, v) for k, v in parse_qsl(query, keep_blank_values=False) if k not in TRACKING_PARAMS]
    return urlencode(kept)


def _youtube_video_id(parts) -> str:
    host = (parts.hostname or "").lower()
    if host == "youtu.be":
        candidate = parts.path.strip("/").split("/")[0]
    else:
        if parts.path.startswith(("/shorts/", "/embed/", "/live/", "/v/")):
            candidate = parts.path.strip("/").split("/")[1]
        else:
            candidate = dict(parse_qsl(parts.query)).get("v", "")
    candidate = candidate.strip()
    if not _VIDEO_ID_RE.match(candidate):
        raise GatekeeperError(
            "youtube_id_not_found",
            "That YouTube link has no single video id in it. Paste the link from the "
            "video page itself (`youtube.com/watch?v=...` or a `youtu.be/...` link).",
        )
    return candidate


def normalize_url(raw_url: str, *, cfg: Settings | None = None) -> NormalizedTarget:
    """Classify and canonicalise a URL, or refuse it with a reason.

    Order matters: scope rules run before SSRF resolution so an obviously
    out-of-scope link (a playlist, an `.mp4`) is rejected without a DNS lookup.
    """
    cfg = cfg or settings()
    raw = (raw_url or "").strip()
    if not raw:
        raise GatekeeperError("empty_url", "No link was provided.")
    if "://" not in raw:
        raw = f"https://{raw}"

    parts = urlsplit(raw)
    host = (parts.hostname or "").lower().rstrip(".")
    if parts.scheme.lower() not in ("http", "https"):
        raise GatekeeperError(
            "scheme_not_allowed",
            f"`{parts.scheme}://` links are not supported — only http and https.",
        )
    if not host:
        raise GatekeeperError("malformed_url", "That link has no hostname.")

    query_params = dict(parse_qsl(parts.query))

    # ---- V1 scope: no raw media files ------------------------------------
    if _MEDIA_EXT_RE.search(raw):
        raise GatekeeperError(
            "raw_media_blocked",
            "Raw audio and video files are out of scope in V1. If it is on YouTube, "
            "paste the YouTube link instead.",
        )

    # ---- YouTube ----------------------------------------------------------
    if host in YOUTUBE_HOSTS:
        if "list" in query_params or parts.path.startswith(("/playlist", "/@", "/channel/", "/c/", "/results")):
            raise GatekeeperError(
                "playlist_blocked",
                "Playlists and channels are out of scope in V1 — paste a single video link.",
            )
        if parts.path.startswith("/live/"):
            raise GatekeeperError(
                "livestream_blocked",
                "Livestreams are out of scope in V1. Once the stream is published as a "
                "normal video, the link will work.",
            )
        video_id = _youtube_video_id(parts)
        canonical = f"https://www.youtube.com/watch?v={video_id}"
        validate_url(canonical)
        return NormalizedTarget(
            source_type="youtube",
            canonical_url=canonical,
            original_url=raw_url,
            identifier=video_id,
        )

    # ---- GitHub -----------------------------------------------------------
    # ---- `R-23`: recognised, and refused for what is actually missing ----
    for hosts, platform in PLATFORM_HOSTS:
        if host in hosts:
            raise GatekeeperError(
                "unsupported_content_type",
                f"Brain does not read {platform} links yet. Paste the transcript "
                f"instead, or the text you want kept.",
            )

    if host in GITHUB_HOSTS:
        canonical = _canonical_github(parts)
        validate_url(canonical)
        return NormalizedTarget(
            source_type="github",
            canonical_url=canonical,
            original_url=raw_url,
            identifier=urlsplit(canonical).path,
        )

    # ---- generic article --------------------------------------------------
    canonical = urlunsplit(
        (
            parts.scheme.lower(),
            host if not parts.port else f"{host}:{parts.port}",
            parts.path or "/",
            _strip_tracking(parts.query),
            "",
        )
    )
    validate_url(canonical)
    return NormalizedTarget(
        source_type="article", canonical_url=canonical, original_url=raw_url
    )


def _canonical_github(parts) -> str:
    """Rewrite a `github.com/.../blob/...` link to its raw equivalent."""
    host = (parts.hostname or "").lower()
    segments = [s for s in parts.path.split("/") if s]
    if host == "raw.githubusercontent.com":
        return urlunsplit(("https", host, parts.path, "", ""))
    if len(segments) >= 5 and segments[2] in ("blob", "raw"):
        owner, repo, _kind, ref, *rest = segments
        return f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{'/'.join(rest)}"
    # A bare repo / tree URL: treat it as an article-style HTML page.
    return urlunsplit(("https", host, parts.path or "/", _strip_tracking(parts.query), ""))


# ---------------------------------------------------------------------------
# duration gate (called once metadata is known)
# ---------------------------------------------------------------------------
def check_duration(duration_sec: int, *, cfg: Settings | None = None) -> int:
    """Enforce the 120-minute ceiling; returns the minutes to reserve."""
    cfg = cfg or settings()
    minutes = max(1, (int(duration_sec or 0) + 59) // 60)
    if minutes > cfg.max_media_minutes:
        raise GatekeeperError(
            "too_long",
            f"That video is {minutes} minutes long; the V1 ceiling is "
            f"{cfg.max_media_minutes} minutes.",
        )
    return minutes


def check_is_live(is_live: bool) -> None:
    if is_live:
        raise GatekeeperError(
            "livestream_blocked",
            "That is a live broadcast. Livestreams are out of scope in V1.",
        )


# ---------------------------------------------------------------------------
# file validation
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ValidatedFile:
    path: Path
    filename: str
    extension: str
    source_type: str
    size_bytes: int
    content_hash: str


def file_source_type(extension: str) -> str:
    if extension == "pdf":
        return "pdf"
    if extension == "docx":
        return "docx"
    if extension in TEXT_EXTS:
        return "text"
    if extension in IMAGE_EXTS:
        return "image"
    raise GatekeeperError("unsupported_file", f"`.{extension}` files are not supported in V1.")


#: The prefix `api_upload` puts on a staged file so two uploads of the same
#: name cannot collide: `up_` + `new_id`'s 20 hex characters + `_`.
#: Pinned to exactly 20 so a person's own `up_abc_notes.txt` survives intact —
#: a looser pattern would quietly rename their file.
_STAGED_RE = re.compile(r"^up_[0-9a-f]{20}_")


def unstage(name: str) -> str:
    """The name the person gave the file, not the one we stored it under.

    The worker re-validates from `ingestion_jobs.input_target`, which is the
    staged path, so without this the source is titled
    `up_0228df82e6454712ad38_board notes.txt` — a machine id the person never
    chose, shown back to them as the name of their own document, and carried
    into the spend list on §7.10.
    """
    return _STAGED_RE.sub("", name)


def validate_file(path: str | Path, *, filename: str | None = None, cfg: Settings | None = None) -> ValidatedFile:
    """Validate an upload: extension, declared size, magic bytes, zip bombs."""
    cfg = cfg or settings()
    file_path = Path(path)
    name = filename or unstage(file_path.name)
    extension = name.rsplit(".", 1)[-1].lower() if "." in name else ""

    if not extension:
        raise GatekeeperError("unsupported_file", "The file has no extension, so its type is unknown.")
    if extension in cfg.blocked_file_exts:
        raise GatekeeperError(
            "raw_media_blocked",
            f"`.{extension}` files (raw audio/video) are out of scope in V1.",
        )
    if extension not in cfg.allowed_file_exts:
        raise GatekeeperError(
            "unsupported_file",
            f"`.{extension}` is not in the V1 file list (pdf, docx, txt, md, png, jpg, webp).",
        )
    if not file_path.is_file():
        raise GatekeeperError("file_missing", "That file could not be read.")

    size = file_path.stat().st_size
    if size == 0:
        raise GatekeeperError("file_empty", "That file is empty.")
    if size > cfg.max_upload_bytes:
        raise GatekeeperError(
            "file_too_large",
            f"That file is {size // (1024 * 1024)}MB; the upload cap is "
            f"{cfg.max_upload_bytes // (1024 * 1024)}MB.",
        )

    head = file_path.read_bytes()[:16] if size < 16 else _read_head(file_path, 16)
    expected = MAGIC.get(extension)
    if expected and not any(head.startswith(prefix) for prefix in expected):
        raise GatekeeperError(
            "content_type_mismatch",
            f"That file is named `.{extension}` but its contents are not a real "
            f"{extension.upper()} file.",
        )
    if extension in TEXT_EXTS:
        _assert_decodable(file_path)
    if extension == "docx":
        inspect_zip(file_path, cfg=cfg)

    return ValidatedFile(
        path=file_path,
        filename=name,
        extension=extension,
        source_type=file_source_type(extension),
        size_bytes=size,
        content_hash=hash_file(file_path),
    )


def _read_head(path: Path, count: int) -> bytes:
    with path.open("rb") as handle:
        return handle.read(count)


def _assert_decodable(path: Path) -> None:
    try:
        path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        try:
            path.read_text(encoding="latin-1")
        except Exception as exc:  # pragma: no cover - defensive
            raise GatekeeperError("undecodable_text", "That text file could not be decoded.") from exc


def inspect_zip(path: Path, *, cfg: Settings | None = None) -> int:
    """Zip-bomb guard for DOCX: cap total inflated size and compression ratio."""
    cfg = cfg or settings()
    try:
        with zipfile.ZipFile(path) as archive:
            total = 0
            for info in archive.infolist():
                total += info.file_size
                if total > cfg.max_docx_decompressed_bytes:
                    raise GatekeeperError(
                        "zip_bomb",
                        "That DOCX expands to more than "
                        f"{cfg.max_docx_decompressed_bytes // (1024 * 1024)}MB and was refused.",
                    )
                if info.compress_size > 0:
                    ratio = info.file_size / info.compress_size
                    if ratio > cfg.max_zip_compression_ratio and info.file_size > 1_000_000:
                        raise GatekeeperError(
                            "zip_bomb",
                            f"`{info.filename}` inside that DOCX has a {ratio:.0f}x "
                            "compression ratio and was refused.",
                        )
            return total
    except zipfile.BadZipFile as exc:
        raise GatekeeperError("corrupt_file", "That DOCX is not a readable ZIP container.") from exc


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(65_536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def hash_text(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()
