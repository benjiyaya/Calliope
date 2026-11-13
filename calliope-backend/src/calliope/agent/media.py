"""Multimodal projection: user attachments and mentions become LLM content.

The runtime builds each subagent's user turn through this module. Image
attachments become OpenAI-style ``image_url`` parts (downscaled data URLs
read from disk), video attachments become evenly-spaced JPEG frames (ffmpeg)
with a timestamp map in the text part, and document attachments (.txt/.md/
.docx) are injected as bounded text between ``[Script document: …]``
delimiters — the agent drafts storylines from a user-written script without
any separate ingestion path.

Mentions (@workflow / @skill) become a ``[Calliope context]`` text appendix:
in the studio engine a workflow mention means the agent prepares a canvas
node + review for it (the review gate is the only queue door), and a skill
mention asks the agent to ``read_skill`` it before writing.

Everything degrades gracefully: a missing file, a path outside ``assets_dir``,
or an unknown extension falls back to a text path line instead of failing
the turn.
"""
from __future__ import annotations

import base64
import json
import logging
from pathlib import Path
from typing import Any

from calliope.config import settings

logger = logging.getLogger("calliope.agent.media")


# ─────────────────────────────────────────────────────────────────────────
# Mentions appendix
# ─────────────────────────────────────────────────────────────────────────


def format_calliope_context(
    mentions: list[dict[str, Any]] | None = None,
    attachments: list[dict[str, Any]] | None = None,
) -> str:
    """Machine appendix projected onto LLM user content (ids, not just names)."""
    wf_lines: list[str] = []
    skill_lines: list[str] = []
    att_lines: list[str] = []
    for m in mentions or []:
        if not isinstance(m, dict):
            continue
        if m.get("type") == "skill":
            name = str(m.get("name") or "").strip()
            if not name:
                continue
            desc = str(m.get("description") or "").replace("\n", " ")[:200]
            skill_lines.append(
                f'skill="{name}"' + (f" — {desc}" if desc else "")
                + " — call read_skill with this name before writing."
            )
            continue
        if m.get("type") not in (None, "workflow"):
            continue
        try:
            wid = int(m["id"])
        except (KeyError, TypeError, ValueError):
            continue
        name = str(m.get("name") or "").replace('"', "'")
        kind = str(m.get("kind") or "image")
        wf_lines.append(
            f'workflow_id={wid} name="{name}" kind={kind}'
            " — the user picked this workflow; create a canvas node for it"
            " (create_canvas_node with this workflow_id) and run_canvas_node"
            " to prepare the review card. The user presses Generate."
        )
    for a in attachments or []:
        if not isinstance(a, dict):
            continue
        path = str(a.get("path") or "").strip()
        if not path:
            continue
        kind = str(a.get("kind") or "image")
        if kind == "document":
            name = str(a.get("name") or "").strip() or Path(path).name
            att_lines.append(
                f'script document: "{name}" — the user uploaded their own script; draft from it'
            )
        else:
            att_lines.append(f"attached: {path} ({kind})")
    # Guardrail: one Calliope workflow per turn so the model cannot fan out
    # canvas runs across several tagged ids.
    lines = ([wf_lines[0]] if wf_lines else []) + skill_lines + att_lines
    if not lines:
        return ""
    return "[Calliope context]\n" + "\n".join(lines)


def project_user_content(
    content: str,
    mentions: list[dict[str, Any]] | None = None,
    attachments: list[dict[str, Any]] | None = None,
) -> str | list[dict[str, Any]]:
    """User prose plus the Calliope context appendix for the LLM.

    Returns a plain string when there are no usable attachments (the common
    text-only case) and an OpenAI parts list otherwise.
    """
    appendix = format_calliope_context(mentions, attachments)
    prose = (content or "").rstrip()
    text = prose
    if appendix:
        text = f"{prose}\n\n{appendix}" if prose else appendix

    image_parts: list[dict[str, Any]] = []
    video_lines: list[str] = []
    document_blocks: list[str] = []
    for a in attachments or []:
        if not isinstance(a, dict):
            continue
        kind = str(a.get("kind") or "image")
        path = str(a.get("path") or "")
        if kind == "image":
            data_url = _image_attachment_data_url(path)
            if data_url:
                image_parts.append(
                    {"type": "image_url", "image_url": {"url": data_url}}
                )
        elif kind == "video":
            frames = _video_attachment_frames(path)
            for ts, data_url in frames:
                image_parts.append(
                    {"type": "image_url", "image_url": {"url": data_url}}
                )
                video_lines.append(f"{path} frame at {ts:.2f}s")
            if not frames:
                continue
        elif kind == "document":
            doc_text = _document_attachment_text(path, str(a.get("name") or ""))
            if doc_text:
                document_blocks.append(doc_text)
    if not image_parts and not document_blocks:
        return text

    frame_map = ""
    if video_lines:
        frame_map = "\n\n[Video frames in order]\n" + "\n".join(video_lines)
    if document_blocks:
        frame_map = "\n\n" + "\n\n".join(document_blocks) + frame_map
    parts: list[dict[str, Any]] = [
        {"type": "text", "text": (text or "(see attached)") + frame_map}
    ]
    parts.extend(image_parts)
    return parts


# ─────────────────────────────────────────────────────────────────────────
# Images
# ─────────────────────────────────────────────────────────────────────────

# Images are downscaled before reaching the LLM context — a full-res PNG can
# be multiple MB of base64, which bloats every subsequent request in the turn.
# The gate is BYTES *and* DIMENSIONS: cloud vision APIs tile by pixels, so a
# 4K image that happens to compress under the byte budget (WebP, hard-squeezed
# JPEG) still costs ~8-20x the vision tokens of a 1024px one if sent as-is.
_MAX_VISION_IMAGE_BYTES = 512_000
_MAX_VISION_EDGE_PX = 1280
_VISION_MIME_BY_EXT = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}

# Encoded data URLs cached per (path, mtime_ns, size). Retries and multi-grant
# turns re-project the same attachments — without this each projection re-reads
# the full-res file, re-decodes 8 megapixels, and re-encodes. Keyed on
# mtime/size so an edited file invalidates itself; bounded LRU (each entry
# ≤ ~700KB base64).
_VISION_CACHE: dict[tuple[str, int, int], str] = {}
_VISION_CACHE_CAP = 32


def _image_attachment_data_url(path: str) -> str | None:
    """Read an attachment image under assets_dir as a downscaled data URL.

    Returns None (logging the reason — the text appendix still names the
    file) when the path is missing/outside assets_dir, not a known image
    type, or too large after decoding.
    """
    raw = str(path or "").strip()
    if not raw:
        return None
    try:
        target = Path(raw).resolve()
        target.relative_to(settings.assets_dir.resolve())
    except (ValueError, OSError):
        return None
    mime = _VISION_MIME_BY_EXT.get(target.suffix.lower())
    if mime is None or not target.is_file():
        return None
    try:
        stat = target.stat()
    except OSError:
        return None
    key = (str(target), stat.st_mtime_ns, stat.st_size)
    cached = _VISION_CACHE.get(key)
    if cached is not None:
        # Refresh insertion order — plain dicts are insertion-ordered, so
        # re-inserting makes this the newest entry (LRU behavior).
        _VISION_CACHE.pop(key, None)
        _VISION_CACHE[key] = cached
        return cached
    data = _downscale_image(target, mime)
    if data is None:
        return None
    data, mime = data
    if not data or len(data) > _MAX_VISION_IMAGE_BYTES:
        return None
    url = f"data:{mime};base64,{base64.b64encode(data).decode()}"
    _VISION_CACHE[key] = url
    while len(_VISION_CACHE) > _VISION_CACHE_CAP:
        _VISION_CACHE.pop(next(iter(_VISION_CACHE)))
    return url


def _downscale_image(target: Path, mime: str) -> tuple[bytes, str] | None:
    """Re-encode oversized images; passes small ones through.

    "Oversized" is bytes > _MAX_VISION_IMAGE_BYTES OR a long edge beyond
    _MAX_VISION_EDGE_PX — a small-file 4K image must still shrink, because
    vision APIs price tiles by pixels, not bytes. Returns (data, mime) — the
    mime is the source extension's for pass-through and ``image/jpeg`` for
    re-encoded output (the caller must not keep labeling JPEG bytes as
    image/png). None when the image cannot be read or re-encoded small
    enough.
    """
    data = target.read_bytes()
    over_bytes = len(data) > _MAX_VISION_IMAGE_BYTES
    over_edge = False
    if not over_bytes:
        # Lazy probe: Image.open reads only the header for .width/.height —
        # no pixel decode. (Cheap here; the data-URL cache in
        # _image_attachment_data_url means this runs once per file anyway.)
        try:
            from PIL import Image

            with Image.open(target) as probe:
                over_edge = (
                    probe.width > _MAX_VISION_EDGE_PX or probe.height > _MAX_VISION_EDGE_PX
                )
        except Exception:
            over_edge = False
    if not over_bytes and not over_edge:
        return data, mime
    try:
        from PIL import Image
    except ImportError:
        logger.error(
            "Vision: cannot downscale %s (%d bytes > %d budget) — Pillow is "
            "not installed. Attachment degrades to a text path line; add the "
            "`pillow` dependency to fix.",
            target.name,
            len(data),
            _MAX_VISION_IMAGE_BYTES,
        )
        return None
    try:
        with Image.open(target) as img:
            img = img.convert("RGB")
            # Adaptive ladder: step quality down, then the LONG edge, until
            # the re-encode fits the budget — dense screenshots can exceed it
            # even at q82/1024px. Scaling the long edge (never upscaling)
            # keeps portrait 4K at ~576x1024 instead of blowing height back
            # out past the cap the way width-first scaling did.
            import io

            for edge, quality in ((1024, 82), (1024, 60), (768, 50), (640, 40)):
                longest = max(img.width, img.height)
                scale = min(1.0, edge / longest)
                resized = img.resize(
                    (max(1, round(img.width * scale)), max(1, round(img.height * scale)))
                )
                buf = io.BytesIO()
                resized.save(buf, format="JPEG", quality=quality)
                if len(buf.getvalue()) <= _MAX_VISION_IMAGE_BYTES:
                    return buf.getvalue(), "image/jpeg"
    except Exception:
        logger.exception("Vision: failed to decode/downscale %s", target)
        return None
    logger.error(
        "Vision: %s still over the %d budget after the full re-encode ladder "
        "— degrading to text path line",
        target.name,
        _MAX_VISION_IMAGE_BYTES,
    )
    return None


# ─────────────────────────────────────────────────────────────────────────
# Video frames
# ─────────────────────────────────────────────────────────────────────────

# Video attachments are "seen" as evenly-spaced frames (frame extraction over
# native video_url — works on ANY vision endpoint). 8 frames is enough for
# blockout motion mapping without drowning the context; each frame reuses the
# image budget.
_MAX_VIDEO_FRAMES = 8
_MAX_VIDEO_FRAME_BYTES = 512_000
_VIDEO_MIME_BY_EXT = {
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mov": "video/quicktime",
    ".mkv": "video/x-matroska",
    ".m4v": "video/x-m4v",
}


def _run_ffmpeg(args: list[str], timeout: float = 30.0) -> bytes | None:
    """Run a short ffmpeg/ffprobe command; stdout bytes or None."""
    import shutil
    import subprocess

    exe = shutil.which(args[0])
    if not exe:
        return None
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [exe, *args[1:]],
            capture_output=True,
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def _ffprobe_duration_seconds(path: Path) -> float | None:
    out = _run_ffmpeg(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
        timeout=15.0,
    )
    if not out:
        return None
    try:
        data = json.loads(out.decode("utf-8", "replace"))
        duration = float(data["format"]["duration"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None
    return duration if duration > 0 else None


def _extract_video_frames(path: Path) -> list[tuple[float, bytes]]:
    """Evenly-spaced JPEG frames + timestamps (seconds), ≤ _MAX_VIDEO_FRAMES.

    Empty list when ffmpeg is unavailable or extraction fails — the caller
    degrades to the path-only text line.
    """
    import tempfile

    if _run_ffmpeg(["ffmpeg", "-version"], timeout=10.0) is None:
        return []
    duration = _ffprobe_duration_seconds(path)
    if duration is None:
        return []
    # timestamped frames via the fps filter: N frames spread across the clip
    fps_expr = f"fps={_MAX_VIDEO_FRAMES}/{duration:.6f}"
    frames: list[tuple[float, bytes]] = []
    with tempfile.TemporaryDirectory(prefix="calliope-vidframes-") as tmp:
        pattern = str(Path(tmp) / "frame_%02d.jpg")
        _run_ffmpeg(
            [
                "ffmpeg", "-y", "-v", "error",
                "-i", str(path),
                "-vf", f"{fps_expr},scale='min(768,iw)':-2",
                "-frames:v", str(_MAX_VIDEO_FRAMES),
                "-q:v", "5",
                pattern,
            ],
            timeout=45.0,
        )
        files = sorted(Path(tmp).glob("frame_*.jpg"))
        for i, f in enumerate(files[:_MAX_VIDEO_FRAMES]):
            data = f.read_bytes()
            if not data or len(data) > _MAX_VIDEO_FRAME_BYTES:
                continue
            # frame k of N evenly spread over duration lands at k*duration/N
            ts = round(i * duration / max(1, len(files)), 2)
            frames.append((ts, data))
    return frames


def _video_attachment_frames(path: str) -> list[tuple[float, str]]:
    """Frame data URLs (+timestamps) for a video attachment under assets_dir.

    Mirrors _image_attachment_data_url's containment rules; returns [] when
    the path is missing/outside assets_dir, not a known video type, or when
    ffmpeg/ffprobe is unavailable.
    """
    raw = str(path or "").strip()
    if not raw:
        return []
    try:
        target = Path(raw).resolve()
        target.relative_to(settings.assets_dir.resolve())
    except (ValueError, OSError):
        return []
    if target.suffix.lower() not in _VIDEO_MIME_BY_EXT or not target.is_file():
        return []
    frames = _extract_video_frames(target)
    return [
        (ts, f"data:image/jpeg;base64,{base64.b64encode(data).decode()}")
        for ts, data in frames
    ]


# ─────────────────────────────────────────────────────────────────────────
# Documents
# ─────────────────────────────────────────────────────────────────────────

# Document attachments (.txt/.md/.docx) are read as text and injected into the
# user turn between delimiters — the whole point is the agent drafting from a
# user-written script without any separate ingestion path.
_MAX_DOCUMENT_CHARS = 60_000
_DOCUMENT_EXTS = {".txt", ".md", ".docx"}


def _extract_docx_text(target: Path) -> str:
    """.docx → text via stdlib zipfile + XML tag-strip (word/document.xml)."""
    import re as _re
    import zipfile

    with zipfile.ZipFile(target) as zf:
        xml = zf.read("word/document.xml").decode("utf-8", errors="replace")
    # Paragraph and break tags become newlines before stripping.
    xml = xml.replace("</w:p>", "\n").replace("<w:br/>", "\n").replace("<w:tab/>", "\t")
    text = _re.sub(r"<[^>]+>", "", xml)
    return text


def _document_attachment_text(path: str, name: str = "") -> str | None:
    """Read a document attachment under assets_dir as bounded plain text.

    Returns None (the appendix still names the file) when the path is
    missing/outside assets_dir or not a known document type. Text is truncated
    at _MAX_DOCUMENT_CHARS with an explicit note so the model knows it saw a
    partial document.
    """
    import zipfile

    raw = str(path or "").strip()
    if not raw:
        return None
    try:
        target = Path(raw).resolve()
        target.relative_to(settings.assets_dir.resolve())
    except (ValueError, OSError):
        return None
    if target.suffix.lower() not in _DOCUMENT_EXTS or not target.is_file():
        return None
    try:
        if target.suffix.lower() == ".docx":
            text = _extract_docx_text(target)
        else:
            text = target.read_text(encoding="utf-8", errors="replace")
    except (OSError, zipfile.BadZipFile):
        return None
    label = name or target.name
    if len(text) > _MAX_DOCUMENT_CHARS:
        return (
            text[:_MAX_DOCUMENT_CHARS]
            + f"\n\n[Document truncated: showing first {_MAX_DOCUMENT_CHARS} of {len(text)} characters]"
        )
    return f"[Script document: {label}]\n{text}\n[/Script document]"
