"""Hardened fetchers and parsers (prod path).

Hard rules enforced here:

* **No shell.** Every subprocess is an argument array with ``shell=False``, a
  timeout, ``--no-config``, a filesize cap, and ``--`` before the URL so a
  crafted link can never be read as a CLI flag.
* **No unchecked network.** All HTTP goes through ``ssrf.safe_get``, which
  disables automatic redirects and re-validates every hop.
* **Hard caps.** 200 PDF pages, 30MB inflated DOCX, 20MB bodies.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from app.config import Settings, settings
from app.ingestion.gatekeeper import GatekeeperError, check_duration, check_is_live
from app.ingestion.providers import FetchedContent, ProviderError
from app.security.sanitizer import clean_text
from app.security.ssrf import SSRFBlocked, safe_get

VTT_TIMESTAMP_RE = re.compile(
    r"(\d{1,2}):(\d{2}):(\d{2})[.,](\d{3})\s*-->\s*(\d{1,2}):(\d{2}):(\d{2})[.,](\d{3})"
)
LOGGER = logging.getLogger("brain.fetchers")

VTT_TAG_RE = re.compile(r"<[^>]+>")
MD_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")


# ---------------------------------------------------------------------------
# yt-dlp
# ---------------------------------------------------------------------------
def build_ytdlp_metadata_cmd(url: str, *, cfg: Settings | None = None) -> list[str]:
    """Argument array for metadata only. ``--`` isolates the URL from flags."""
    cfg = cfg or settings()
    return [
        "yt-dlp",
        "--no-config",
        # `--no-exec` cancels any --exec that a config or a crafted URL
        # could otherwise smuggle in, so yt-dlp can never spawn a shell.
        "--no-exec",
        "--no-playlist",
        "--skip-download",
        "--dump-single-json",
        "--max-filesize", cfg.ytdlp_max_filesize,
        "--socket-timeout", "15",
        "--retries", "2",
        "--",
        url,
    ]


def build_ytdlp_subtitle_cmd(url: str, outdir: str | Path, *, cfg: Settings | None = None) -> list[str]:
    cfg = cfg or settings()
    return [
        "yt-dlp",
        "--no-config",
        # `--no-exec` cancels any --exec that a config or a crafted URL
        # could otherwise smuggle in, so yt-dlp can never spawn a shell.
        "--no-exec",
        "--no-playlist",
        "--skip-download",
        "--write-subs",
        "--write-auto-subs",
        "--sub-langs", "en.*,en",
        "--sub-format", "vtt",
        "--max-filesize", cfg.ytdlp_max_filesize,
        "--socket-timeout", "15",
        "--retries", "2",
        "--output", str(Path(outdir) / "%(id)s.%(ext)s"),
        "--",
        url,
    ]


def scrubbed_env() -> dict[str, str]:
    """The only variables a fetcher subprocess is allowed to see.

    `subprocess.run` inherits the parent environment by default, which on this
    server means handing `yt-dlp` — a large third-party program parsing
    attacker-supplied pages — `LLM_API_KEY`, `BRAIN_JWT_SECRET` and every R2
    credential. It needs none of them. `HOME` is redirected to a scratch
    directory so nothing reads or writes a real dotfile, and `PATH` is kept only
    so the binary itself can be found.
    """
    return {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": tempfile.gettempdir(),
        "TMPDIR": tempfile.gettempdir(),
        "LC_ALL": "C.UTF-8",
        "LANG": "C.UTF-8",
    }


def _run(cmd: list[str], *, cfg: Settings) -> subprocess.CompletedProcess:
    if not isinstance(cmd, list) or not all(isinstance(part, str) for part in cmd):
        raise ValueError("subprocess commands must be argument arrays")
    try:
        return subprocess.run(  # noqa: S603 - argument array, shell=False, timeout set
            cmd,
            shell=False,
            capture_output=True,
            text=True,
            timeout=cfg.subprocess_timeout_sec,
            check=False,
            env=scrubbed_env(),
            cwd=tempfile.gettempdir(),
        )
    except FileNotFoundError as exc:
        raise ProviderError("tool_missing", f"`{cmd[0]}` is not installed on this host.") from exc
    except subprocess.TimeoutExpired as exc:
        raise ProviderError(
            "timeout", f"`{cmd[0]}` did not finish within {cfg.subprocess_timeout_sec}s."
        ) from exc


def parse_vtt(text: str) -> list[dict[str, Any]]:
    """Parse WebVTT into ``[{start, end, text}]``, de-duplicating rolling captions."""
    cues: list[dict[str, Any]] = []
    start = end = None
    buffer: list[str] = []

    def flush() -> None:
        nonlocal start, end, buffer
        if start is not None and buffer:
            body = clean_text(" ".join(buffer))
            if body and (not cues or cues[-1]["text"] != body):
                cues.append({"start": start, "end": end, "text": body})
        buffer = []

    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        match = VTT_TIMESTAMP_RE.search(line)
        if match:
            flush()
            h1, m1, s1, ms1, h2, m2, s2, ms2 = (int(g) for g in match.groups())
            start = h1 * 3600 + m1 * 60 + s1
            end = h2 * 3600 + m2 * 60 + s2
            continue
        if not line or line.startswith(("WEBVTT", "NOTE", "STYLE", "Kind:", "Language:")):
            continue
        if line.isdigit():
            continue
        cleaned = VTT_TAG_RE.sub("", line).strip()
        if cleaned and cleaned not in buffer:
            buffer.append(cleaned)
    flush()
    return cues


#: What YouTube says when it is refusing the *caller*, not the video.
#: A datacentre IP gets this; a person on a laptop does not.
_BOT_WALL = (
    "sign in to confirm",
    "not a bot",
    "failed to extract any player response",
    "unable to download api page",
)
#: What it says when the video itself is genuinely unavailable to anyone.
_VIDEO_WALL = (
    "private video",
    "members-only",
    "age-restricted",
    "video unavailable",
    "removed by the uploader",
)


def _youtube_refusal(stderr: str, url: str) -> ProviderError:
    """Tell apart "this video is private" from "YouTube refused this server".

    The old test was `any(k in stderr for k in ("private", "members-only",
    "sign in", "age"))`, and both halves of it were wrong.

    `"sign in"` matches "Sign in to confirm you're not a bot" — YouTube
    blocking the *caller*, which is what a datacentre IP gets. Reporting that
    as "private, members-only or age-restricted" sends the owner to check a
    video that is perfectly public, and nothing they do to it will help.
    Three of the most public videos on YouTube failed that way in production.

    `"age"` is a bare substring, so it also matched "message", "usage" and
    "package" — any unrelated error carrying one of those words was reported
    as an age restriction.

    The raw stderr was thrown away, which is why none of this was visible.
    """
    text = (stderr or "").lower()
    head = " ".join((stderr or "").split())[:400]
    # Ours before theirs. Production hit this one and my own classifier called
    # it a bot wall: yt-dlp needs a JavaScript runtime to read YouTube now, the
    # image had none, and every video failed with "No title found in player
    # response". Blaming YouTube for a missing dependency sends the operator
    # looking outward at something they own.
    if "javascript runtime" in text or "js runtime" in text:
        LOGGER.error(
            "yt-dlp has no JavaScript runtime, so YouTube cannot be read at all — "
            "install deno in the image. yt-dlp said: %s", head,
        )
        return ProviderError(
            "js_runtime_missing",
            "This Brain cannot read YouTube right now: a tool is missing on the server. "
            "Nothing you can fix.",
        )
    if any(k in text for k in _BOT_WALL):
        LOGGER.warning("youtube refused this server for %s — yt-dlp said: %s", url, head)
        return ProviderError(
            "platform_blocked_caller",
            "YouTube refused this server, not the video. The video is probably fine; "
            "paste its transcript instead, or try again later.",
        )
    if any(k in text for k in _VIDEO_WALL):
        LOGGER.info("youtube refused the video %s — yt-dlp said: %s", url, head)
        return ProviderError(
            "blocked_by_platform",
            "YouTube refused that video (private, members-only or age-restricted).",
        )
    LOGGER.warning("yt-dlp failed on %s — stderr: %s", url, head)
    return ProviderError("fetch_failed", "yt-dlp could not read that video's metadata.")


def fetch_youtube(target, *, cfg: Settings | None = None) -> FetchedContent:
    cfg = cfg or settings()
    meta_proc = _run(build_ytdlp_metadata_cmd(target.canonical_url, cfg=cfg), cfg=cfg)
    if meta_proc.returncode != 0:
        raise _youtube_refusal(meta_proc.stderr or "", target.canonical_url)

    try:
        meta = json.loads(meta_proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise ProviderError("fetch_failed", "yt-dlp returned unreadable metadata.") from exc

    check_is_live(bool(meta.get("is_live") or meta.get("live_status") == "is_live"))
    duration = int(meta.get("duration") or 0)
    check_duration(duration, cfg=cfg)

    with tempfile.TemporaryDirectory(prefix="brain-yt-") as tmp:
        sub_proc = _run(build_ytdlp_subtitle_cmd(target.canonical_url, tmp, cfg=cfg), cfg=cfg)
        vtt_files = sorted(Path(tmp).glob("*.vtt"))
        if not vtt_files:
            raise ProviderError(
                "no_transcript",
                "That video has no captions, and V1 does not transcribe audio.",
            )
        cues = parse_vtt(vtt_files[0].read_text(encoding="utf-8", errors="replace"))
        _ = sub_proc  # return code is advisory; the .vtt is the real signal

    if not cues:
        raise ProviderError("no_transcript", "That video's caption file was empty.")

    return FetchedContent(
        title=meta.get("title") or target.identifier,
        full_text="\n".join(cue["text"] for cue in cues),
        cues=cues,
        author=meta.get("uploader") or meta.get("channel") or "",
        published_at=str(meta.get("upload_date") or "")[:10],
        language=(meta.get("language") or "en")[:16],
        duration_sec=duration,
        source_type="youtube",
        locator_type="timestamp",
    ).finalise()


# ---------------------------------------------------------------------------
# HTML / markdown
# ---------------------------------------------------------------------------
def _http_get(url: str, *, cfg: Settings) -> tuple[str, str, bytes]:
    try:
        return safe_get(url, max_bytes=cfg.max_fetch_bytes)
    except SSRFBlocked as exc:
        raise ProviderError("blocked_url", f"That link was refused: {exc.reason}.") from exc
    except Exception as exc:  # network / HTTP status
        raise ProviderError("fetch_failed", f"Could not fetch that link: {exc}") from exc


def html_to_sections(html: str) -> tuple[str, str, list[dict[str, Any]]]:
    """Extract (title, author, sections) from HTML, dropping chrome and scripts."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript", "nav", "footer", "header", "aside", "form"]):
        tag.decompose()

    title = ""
    if soup.title and soup.title.string:
        title = soup.title.string.strip()
    og_title = soup.find("meta", attrs={"property": "og:title"})
    if og_title and og_title.get("content"):
        title = og_title["content"].strip()

    author = ""
    for finder in (
        {"name": "author"},
        {"property": "article:author"},
        {"name": "twitter:creator"},
    ):
        tag = soup.find("meta", attrs=finder)
        if tag and tag.get("content"):
            author = tag["content"].strip()
            break

    root = soup.find("article") or soup.find("main") or soup.body or soup
    sections: list[dict[str, Any]] = []
    current = "Introduction"
    chunk: list[str] = []

    def flush() -> None:
        nonlocal chunk
        body = clean_text(" ".join(chunk))
        if len(body) > 40:
            sections.append({"section": current, "text": body})
        chunk = []

    for element in root.find_all(["h1", "h2", "h3", "p", "li", "blockquote"]):
        text = element.get_text(" ", strip=True)
        if not text:
            continue
        if element.name in ("h1", "h2", "h3"):
            flush()
            current = clean_text(text)[:120] or current
        else:
            chunk.append(text)
    flush()
    return title, author, sections


def fetch_article(target, *, cfg: Settings | None = None) -> FetchedContent:
    cfg = cfg or settings()
    final_url, content_type, body = _http_get(target.canonical_url, cfg=cfg)
    lowered = content_type.lower()

    if "pdf" in lowered or final_url.lower().endswith(".pdf"):
        return _pdf_from_bytes(body, title=target.canonical_url, cfg=cfg)
    if "html" not in lowered and "xml" not in lowered and "text" not in lowered:
        raise ProviderError(
            "unsupported_content_type",
            f"That link returned `{content_type or 'unknown'}`, which V1 does not read.",
        )

    html = body.decode("utf-8", errors="replace")
    title, author, sections = html_to_sections(html)
    if not sections:
        raise ProviderError("empty_content", "No readable article text was found at that link.")
    return FetchedContent(
        title=title or target.canonical_url,
        full_text="\n\n".join(s["text"] for s in sections),
        cues=sections,
        author=author,
        source_type="article",
        locator_type="section",
    ).finalise()


def markdown_to_sections(text: str) -> list[dict[str, Any]]:
    sections: list[dict[str, Any]] = []
    current = "Top"
    chunk: list[str] = []

    def flush() -> None:
        nonlocal chunk
        body = clean_text("\n".join(chunk))
        if body:
            sections.append({"section": current, "text": body})
        chunk = []

    for line in (text or "").splitlines():
        heading = MD_HEADING_RE.match(line)
        if heading:
            flush()
            current = clean_text(heading.group(2))[:120] or current
        else:
            chunk.append(line)
    flush()
    return sections


def fetch_github(target, *, cfg: Settings | None = None) -> FetchedContent:
    cfg = cfg or settings()
    final_url, content_type, body = _http_get(target.canonical_url, cfg=cfg)
    if "html" in content_type.lower():
        title, author, sections = html_to_sections(body.decode("utf-8", errors="replace"))
        if not sections:
            raise ProviderError("empty_content", "That GitHub page had no readable text.")
        return FetchedContent(
            title=title or target.canonical_url,
            full_text="\n\n".join(s["text"] for s in sections),
            cues=sections,
            author=author,
            source_type="github",
            locator_type="section",
        ).finalise()

    text = body.decode("utf-8", errors="replace")
    sections = markdown_to_sections(text)
    if not sections:
        raise ProviderError("empty_content", "That GitHub file was empty.")
    path_parts = [p for p in target.identifier.split("/") if p]
    return FetchedContent(
        title=path_parts[-1] if path_parts else target.canonical_url,
        full_text=clean_text(text),
        cues=sections,
        author="/".join(path_parts[:2]),
        source_type="github",
        locator_type="section",
    ).finalise()


# ---------------------------------------------------------------------------
# uploads
# ---------------------------------------------------------------------------
def parse_text_file(validated, *, cfg: Settings | None = None) -> FetchedContent:
    cfg = cfg or settings()
    raw = validated.path.read_text(encoding="utf-8", errors="replace")
    if validated.extension == "md":
        sections = markdown_to_sections(raw)
    else:
        sections = [
            {"section": f"Part {i + 1}", "text": clean_text(block)}
            for i, block in enumerate(re.split(r"\n{2,}", raw))
            if clean_text(block)
        ]
    if not sections:
        raise ProviderError("empty_content", "That file had no readable text.")
    return FetchedContent(
        title=validated.filename,
        full_text=clean_text(raw),
        cues=sections,
        source_type="text",
        locator_type="section",
        content_hash=validated.content_hash,
    ).finalise()


def _pdf_from_bytes(data: bytes, *, title: str, cfg: Settings) -> FetchedContent:
    import io

    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(data))
    except Exception as exc:
        raise ProviderError("corrupt_file", "That PDF could not be parsed.") from exc

    page_count = len(reader.pages)
    if page_count > cfg.max_pdf_pages:
        raise GatekeeperError(
            "too_many_pages",
            f"That PDF has {page_count} pages; the V1 ceiling is {cfg.max_pdf_pages}.",
        )
    cues: list[dict[str, Any]] = []
    for index, page in enumerate(reader.pages, start=1):
        try:
            text = clean_text(page.extract_text() or "")
        except Exception:
            text = ""
        if text:
            cues.append({"page": index, "text": text})
    if not cues:
        raise ProviderError(
            "no_text_layer",
            "That PDF has no extractable text layer (it is probably a scan), and V1 "
            "does not run OCR.",
        )
    return FetchedContent(
        title=title,
        full_text="\n\n".join(c["text"] for c in cues),
        cues=cues,
        source_type="pdf",
        locator_type="page",
        metadata={"pages": page_count},
    ).finalise()


def parse_pdf_file(validated, *, cfg: Settings | None = None) -> FetchedContent:
    cfg = cfg or settings()
    content = _pdf_from_bytes(validated.path.read_bytes(), title=validated.filename, cfg=cfg)
    content.content_hash = validated.content_hash
    return content


def parse_docx_file(validated, *, cfg: Settings | None = None) -> FetchedContent:
    """Parse a DOCX whose ZIP was already screened for bombs by the gatekeeper."""
    cfg = cfg or settings()
    from docx import Document

    try:
        document = Document(str(validated.path))
    except Exception as exc:
        raise ProviderError("corrupt_file", "That DOCX could not be parsed.") from exc

    sections: list[dict[str, Any]] = []
    current = "Top"
    chunk: list[str] = []

    def flush() -> None:
        nonlocal chunk
        body = clean_text("\n".join(chunk))
        if body:
            sections.append({"section": current, "text": body})
        chunk = []

    for paragraph in document.paragraphs:
        text = (paragraph.text or "").strip()
        if not text:
            continue
        if (paragraph.style.name or "").lower().startswith("heading"):
            flush()
            current = clean_text(text)[:120] or current
        else:
            chunk.append(text)
    flush()

    if not sections:
        raise ProviderError("empty_content", "That DOCX had no readable paragraphs.")
    return FetchedContent(
        title=validated.filename,
        full_text="\n\n".join(s["text"] for s in sections),
        cues=sections,
        source_type="docx",
        locator_type="section",
        content_hash=validated.content_hash,
    ).finalise()


def describe_image_file(validated, *, cfg: Settings | None = None) -> FetchedContent:
    """Transcribe and describe an image with the configured vision provider.

    Raises `vision_unavailable` when no vision key is configured, rather than
    inventing a description — an image nobody can read is not a source.
    """
    from app.ingestion.providers import LiveVisionProvider

    cfg = cfg or settings()
    if not cfg.live_vision:
        raise ProviderError(
            "vision_unavailable",
            "Image ingestion needs a vision model; set LLM_API_KEY to enable it.",
        )
    return LiveVisionProvider(cfg).describe(validated)
