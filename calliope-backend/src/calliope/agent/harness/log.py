"""Append-only session event log — the single source of truth.

Ported from the deepseek-harness session store: an agent session is an
append-only log of typed `SessionEvent`s. LLM history is *derived* from the
log (`derive_llm_history`), never stored separately; chat rows for the UI are
derived too (`derive_chat_rows` — one row per user/assistant/tool fact, the
same shape agent_messages rows had).

Events are persisted in `agent_events` (per session, ordered by seq). Legacy
sessions that only have `agent_messages` are backfilled on first read, so old
chats keep working.
"""
from __future__ import annotations

import base64
import json
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from calliope.config import settings
from calliope.db import get_db, row_to_dict

logger = logging.getLogger("calliope.harness.log")


# Event types (a closed vocabulary; new types may be added, readers ignore
# unknown ones — same growth rule as deepseek-harness SessionEventMap).
TURN_START = "turn/start"
TURN_END = "turn/end"
STEP_START = "step/start"
STEP_END = "step/end"
USER_MESSAGE = "user/message"
ASSISTANT_MESSAGE = "assistant/message"
TOOL_CALL = "tool/call"
TOOL_RESULT = "tool/result"
# Swarm planner events: the plan (task list) + per-task lifecycle markers.
PLAN_CREATED = "plan/created"
TASK_START = "task/start"
TASK_END = "task/end"
# HITL question cards: the agent asks, the user answers by clicking an option
# (or replying in prose). The card is derived state — a question with no
# matching answer yet renders as actionable in the chat UI.
QUESTION_ASKED = "question/asked"
QUESTION_ANSWERED = "question/answered"
# Memory lifecycle: writes are recorded, reads are not (render-time
# use_count bumps are statistics, not session history).
MEMORY_SAVED = "memory/saved"
MEMORY_FORGOTTEN = "memory/forgotten"
# Mid-run steering: the user course-corrects WHILE a turn is executing.
# Deliberately NOT user/message — policy derives render/destructive approval
# from the latest user/message, so steering must never grant or void
# permissions. Loops drain these between steps and inject them as context.
STEERING_MESSAGE = "steering/message"

TOOL_RESULT_TRUNCATE = 4000
# Appended wherever a tool result is cut for the LLM. Must TEACH the way out —
# a bare "[truncated]" sends agents into retry loops hoping for a different
# cut (observed live 2026-08-24: an assets sub-agent re-called get_workspace
# repeatedly, blind to characters/locations below the cut).
TRUNCATE_NOTE = (
    "…[truncated — the full result is too large for one reply. Fetch a "
    "smaller slice instead of retrying: get_workspace accepts "
    "sections=[\"characters\",\"locations\",\"items\",\"beats\",\"scenes\"], "
    "and scoped tools (get_story, list_scenes) return less.]"
)

# Cap for ANY single event payload at append time: keeps the append-only log
# faithful in shape but bounded in size (a 500 KB tool result would otherwise
# be re-sent to the LLM / re-read by the UI on every turn).
EVENT_DATA_TRUNCATE = 16_000


def _bounded_data(data: dict[str, Any]) -> dict[str, Any]:
    """Cap oversized payload values (tool results mainly) in place-ish."""
    out: dict[str, Any] = {}
    for k, v in data.items():
        if k == "result" and v is not None:
            text = json.dumps(v, ensure_ascii=False, default=str)
            if len(text) > EVENT_DATA_TRUNCATE:
                out[k] = {
                    "truncated": True,
                    "preview": text[:EVENT_DATA_TRUNCATE] + "…[truncated]",
                }
                continue
        out[k] = v
    return out


@dataclass
class SessionEvent:
    seq: int
    type: str
    data: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"seq": self.seq, "type": self.type, "data": self.data}


def _db() -> sqlite3.Connection:
    return get_db(settings.db_path)


# ─────────────────────────────────────────────────────────────────────────
# Append / read
# ─────────────────────────────────────────────────────────────────────────


def append_event(session_id: int, event_type: str, data: dict[str, Any]) -> SessionEvent:
    # Atomic single-statement seq allocation: concurrent appenders can never
    # observe the same MAX(seq) (the read-and-write is one INSERT..SELECT
    # under SQLite's write lock) and cannot collide on UNIQUE(session_id, seq).
    data = _bounded_data(data)
    conn = _db()
    try:
        cur = conn.execute(
            """
            INSERT INTO agent_events (session_id, seq, type, data_json)
            VALUES (?, COALESCE((SELECT MAX(seq) FROM agent_events WHERE session_id = ?), 0) + 1, ?, ?)
            """,
            (session_id, session_id, event_type, json.dumps(data, ensure_ascii=False, default=str)),
        )
        # cur.lastrowid is the table-wide row id, NOT the per-session seq the
        # INSERT just allocated; read the seq back. Returning the row id made
        # ask_user hand the card a question_seq that latest_open_question()
        # (which compares per-session seq) could never match, so a card click
        # never wrote question/answered once more than one session existed.
        seq = conn.execute(
            "SELECT seq FROM agent_events WHERE id = ?", (cur.lastrowid,)
        ).fetchone()["seq"]
        conn.execute(
            "UPDATE agent_sessions SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (session_id,),
        )
        conn.commit()
        return SessionEvent(seq=seq, type=event_type, data=data)
    finally:
        conn.close()


def read_events(session_id: int) -> list[SessionEvent]:
    conn = _db()
    try:
        rows = conn.execute(
            "SELECT * FROM agent_events WHERE session_id = ? ORDER BY seq", (session_id,)
        ).fetchall()
        events: list[SessionEvent] = []
        for r in rows:
            try:
                data = json.loads(r["data_json"])
            except (json.JSONDecodeError, TypeError):
                data = {}
            events.append(SessionEvent(seq=r["seq"], type=r["type"], data=data))
        return events
    finally:
        conn.close()


def latest_user_message(session_id: int) -> str | None:
    """Most recent `user/message` content for a session (or None).

    Returns the user's visible prose only — never the machine `[Calliope
    context]` appendix (which can contain `kind=image` and would otherwise
    auto-approve HITL renders). The mentions payload (the `@workflow` tag
    itself) is NOT part of content — policy reads it via
    `latest_user_has_workflow_tag`.
    """
    conn = _db()
    try:
        row = conn.execute(
            "SELECT data_json FROM agent_events "
            "WHERE session_id = ? AND type = ? ORDER BY seq DESC LIMIT 1",
            (session_id, USER_MESSAGE),
        ).fetchone()
        if not row:
            return None
        try:
            data = json.loads(row["data_json"])
        except (json.JSONDecodeError, TypeError):
            return None
        content = data.get("content")
        if not isinstance(content, str):
            return None
        # Never treat the machine appendix as user intent (kind=image, etc.).
        prose = content.split("[Calliope context]", 1)[0].strip()
        return prose or None
    finally:
        conn.close()


def latest_user_has_workflow_tag(session_id: int) -> bool:
    """True when the most recent user/message event carried a workflow
    mention (the @ tag). The tag is a deliberate UI act — the user picked a
    workflow from the picker — so it grants render permission for the turn,
    regardless of whether the prose also contains render verbs.

    Reads the mentions payload, never the prose: "create a Misc. Item
    @SomeWorkflow" with no generate intent should not enqueue — but the
    tag names the workflow, so the text-edits-only assumption is wrong.
    Callers decide scope; this only answers 'did the user tag a workflow?'.
    """
    conn = _db()
    try:
        row = conn.execute(
            "SELECT data_json FROM agent_events "
            "WHERE session_id = ? AND type = ? ORDER BY seq DESC LIMIT 1",
            (session_id, USER_MESSAGE),
        ).fetchone()
        if not row:
            return False
        try:
            data = json.loads(row["data_json"])
        except (json.JSONDecodeError, TypeError):
            return False
        mentions = data.get("mentions")
        if not isinstance(mentions, list):
            return False
        return any(
            isinstance(m, dict) and m.get("type") in (None, "workflow") for m in mentions
        )
    finally:
        conn.close()


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
            skill_lines.append(f'skill="{name}"' + (f' — {desc}' if desc else ""))
            continue
        if m.get("type") not in (None, "workflow"):
            continue
        try:
            wid = int(m["id"])
        except (KeyError, TypeError, ValueError):
            continue
        name = str(m.get("name") or "").replace('"', "'")
        kind = str(m.get("kind") or "image")
        wf_lines.append(f'workflow_id={wid} name="{name}" kind={kind}')
    for a in attachments or []:
        if not isinstance(a, dict):
            continue
        path = str(a.get("path") or "").strip()
        if not path:
            continue
        kind = str(a.get("kind") or "image")
        if kind == "document":
            name = str(a.get("name") or "").strip() or Path(path).name
            att_lines.append(f'script document: "{name}" — the user uploaded their own script; draft from it')
        else:
            att_lines.append(f"attached: {path} ({kind})")
    # Guardrail: one Calliope workflow per turn so the model cannot fan out
    # run_workflow across several tagged ids.
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

    Image attachments become OpenAI-style ``image_url`` content parts (data
    URLs read from disk), so vision-capable models see the actual pixels
    instead of just a path line. Video attachments become evenly-spaced JPEG
    frames (ffmpeg), each an ``image_url`` part, with a timestamp map in the
    text part so the model can place motion on a timeline. Returns a plain
    string when there are no usable attachments (the common text-only case).
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


# Images are downscaled before reaching the LLM context — a full-res PNG can
# be multiple MB of base64, which bloats every subsequent request in the turn.
_MAX_VISION_IMAGE_BYTES = 512_000
_VISION_MIME_BY_EXT = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


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
    data = _downscale_image(target, mime)
    if data is None:
        return None
    data, mime = data
    if not data or len(data) > _MAX_VISION_IMAGE_BYTES:
        return None
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


# Video attachments are "seen" as evenly-spaced frames (user choice: frame
# extraction over native video_url — works on ANY vision endpoint). 8 frames
# is enough for blockout motion mapping without drowning the context; each
# frame reuses the image budget.
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


def _downscale_image(target: Path, mime: str) -> tuple[bytes, str] | None:
    """Re-encode large images at reduced width; passes small ones through.

    Returns (data, mime) — the mime is the source extension's for pass-through
    and ``image/jpeg`` for re-encoded output (the caller must not keep
    labeling JPEG bytes as image/png). None when the image cannot be read or
    re-encoded small enough.
    """
    data = target.read_bytes()
    if len(data) <= _MAX_VISION_IMAGE_BYTES:
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
            # Adaptive ladder: step quality down, then width, until the
            # re-encode fits the budget — dense screenshots can exceed it
            # even at q82/1024px.
            import io

            for width, quality in ((1024, 82), (1024, 60), (768, 50), (640, 40)):
                height = max(1, round(img.height * width / img.width))
                resized = img.resize((width, height))
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


def max_turn_number(session_id: int) -> int:
    """Highest recorded turn number for the session (0 when none).

    Cheaper than read_events for turn numbering: reads only turn/start rows,
    not every event payload in the session.
    """
    conn = _db()
    try:
        rows = conn.execute(
            "SELECT data_json FROM agent_events WHERE session_id = ? AND type = ?",
            (session_id, TURN_START),
        ).fetchall()
        best = 0
        for r in rows:
            try:
                turn = int(json.loads(r["data_json"]).get("turn", 0))
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
            best = max(best, turn)
        return best
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────────
# Mid-run steering
# ─────────────────────────────────────────────────────────────────────────

STEERING_INJECT_HEADER = "[STEERING — user message sent while you work]"


def steering_user_content(d: dict[str, Any]) -> Any:
    """Projected steering content with the [STEERING] header.

    Same shape rules as a user message (string, or multimodal parts when
    attachments are present) — the header is prepended to the text so both
    the live injection (loop drains between steps) and the replayed history
    projection show identical text.
    """
    projected = project_user_content(
        d.get("content") or "",
        d.get("mentions"),
        d.get("attachments"),
    )
    if isinstance(projected, list):
        for part in projected:
            if part.get("type") == "text":
                part["text"] = f"{STEERING_INJECT_HEADER}\n{part.get('text') or ''}"
                return projected
        return [{"type": "text", "text": STEERING_INJECT_HEADER}, *projected]
    return f"{STEERING_INJECT_HEADER}\n{projected}" if projected else STEERING_INJECT_HEADER


def drain_steering(session_id: int, after_seq: int) -> list[SessionEvent]:
    """Unconsumed steering events with seq > after_seq, oldest first.

    Loops keep a per-turn watermark: steering drained once is never re-drained
    (injection appends the text to the in-flight `messages` list directly, so
    it lives on in that request history without a re-read)."""
    conn = _db()
    try:
        rows = conn.execute(
            "SELECT * FROM agent_events WHERE session_id = ? AND type = ? AND seq > ? ORDER BY seq",
            (session_id, STEERING_MESSAGE, after_seq),
        ).fetchall()
    finally:
        conn.close()
    out: list[SessionEvent] = []
    for r in rows:
        try:
            data = json.loads(r["data_json"])
        except (json.JSONDecodeError, TypeError):
            data = {}
        out.append(SessionEvent(seq=r["seq"], type=r["type"], data=data))
    return out


def steering_max_seq(session_id: int) -> int:
    """Highest steering/message seq for the session (watermark seed)."""
    conn = _db()
    try:
        row = conn.execute(
            "SELECT MAX(seq) AS m FROM agent_events WHERE session_id = ? AND type = ?",
            (session_id, STEERING_MESSAGE),
        ).fetchone()
        return int(row["m"]) if row and row["m"] is not None else 0
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────────
# Derivations
# ─────────────────────────────────────────────────────────────────────────


def derive_llm_history(
    events: list[SessionEvent],
    max_user_turns: int | None = None,
    max_chars: int | None = None,
) -> list[dict[str, Any]]:
    """Project OpenAI-format LLM history from the event log.

    Rules:
    - user/message   → user role
    - assistant/message → assistant role (content + tool_calls)
    - tool/result    → tool role with tool_call_id (attached to the step's
                       tool/call events)
    - Turn/step boundaries and chunks are replay-only; they do not project.
    - agent_name is flattened into the content for sub-agent turns ([name] prefix)

    `max_user_turns` bounds the projection to the last N user messages (and
    everything after each, so tool-call/result pairs always stay complete —
    exchanges never span user turns). None = unbounded (legacy behavior).

    `max_chars` is a secondary character budget over the assembled history:
    when over budget, oldest WHOLE turns drop (never mid-turn — pairs stay
    intact) until it fits or only the latest turn remains. None = unbounded.

    `steering/message` events (mid-run course corrections) project as user
    messages so the correction stays in the conversation record — but NEVER
    inside an open tool exchange: a user message between an assistant
    tool_calls message and its tool results is an invalid request sequence
    (OpenAI-compatible servers 400 the whole next turn), so steering that
    landed mid-exchange buffers until the exchange closes. Steering never
    opens a user-turn boundary: it belongs to the turn it interrupted, and
    `policy.latest_user_message` reads only user/message events — steering
    cannot grant or void render/destructive approval.
    """
    history: list[dict[str, Any]] = []
    user_turn_boundaries: list[int] = []
    tool_call_by_id: dict[str, dict[str, Any]] = {}
    # Open tool exchange: call ids of the latest assistant tool_calls batch
    # whose results have not all landed. Steering that arrives mid-exchange
    # buffers here (a user message inside the pair is an invalid request).
    pending_call_ids: set[str] = set()
    steer_buffer: list[dict[str, Any]] = []

    def _flush_steering() -> None:
        for d in steer_buffer:
            history.append({"role": "user", "content": steering_user_content(d)})
        steer_buffer.clear()

    for e in events:
        d = e.data
        if e.type == USER_MESSAGE:
            _flush_steering()  # never reorder steering after a later user turn
            user_turn_boundaries.append(len(history))
            history.append(
                {
                    "role": "user",
                    "content": project_user_content(
                        d.get("content") or "",
                        d.get("mentions"),
                        d.get("attachments"),
                    ),
                }
            )
        elif e.type == STEERING_MESSAGE:
            if (d.get("content") or "").strip() or d.get("attachments"):
                steer_buffer.append(d)
        elif e.type == ASSISTANT_MESSAGE:
            msg: dict[str, Any] = {"role": "assistant"}
            name = d.get("agent_name")
            content = d.get("content") or ""
            if name:
                msg["content"] = f"[{name}] {content}" if content else f"[{name}]"
            else:
                msg["content"] = content
            tool_calls = d.get("tool_calls") or []
            if tool_calls:
                msg["tool_calls"] = tool_calls
                for tc in tool_calls:
                    cid = tc.get("id")
                    if cid:
                        pending_call_ids.add(cid)
            history.append(msg)
        elif e.type == TOOL_CALL:
            tool_call_by_id[d.get("call_id", "")] = d
        elif e.type == TOOL_RESULT:
            call = tool_call_by_id.get(d.get("call_id", ""))
            tool_name = d.get("tool_name") or (call or {}).get("tool_name") or "tool"
            result = d.get("result") or {}
            digest = _truncate_result(result)
            history.append(
                {
                    "role": "tool",
                    "tool_call_id": d.get("call_id", ""),
                    "content": f"[{tool_name}] {digest}",
                }
            )
            pending_call_ids.discard(d.get("call_id", ""))
            if not pending_call_ids:
                _flush_steering()  # exchange closed — safe injection point
    # Steering that never saw its exchange close (crashed turn) still projects.
    _flush_steering()
    if max_user_turns is not None and user_turn_boundaries:
        # Keep the last N user turns (and everything after each boundary —
        # tool exchanges never span user turns, so pairs stay intact).
        # Clamp N to the actual turn count: `[-N]` on a shorter list raises
        # IndexError, which a fresh 1-turn session hit (N=40 >> 1 turn).
        if max_user_turns <= 0:
            start = len(history)
        else:
            start = user_turn_boundaries[-min(max_user_turns, len(user_turn_boundaries))]
        if start > 0:
            history = history[start:]
            user_turn_boundaries = [b - start for b in user_turn_boundaries if b >= start]
    if max_chars is not None and max_chars > 0:
        history = _trim_history_to_char_budget(history, user_turn_boundaries, max_chars)
    return history


def _history_len(history: list[dict[str, Any]]) -> int:
    return sum(len(m.get("content") or "") for m in history)


def _trim_history_to_char_budget(
    history: list[dict[str, Any]],
    user_turn_boundaries: list[int],
    max_chars: int,
) -> list[dict[str, Any]]:
    """Drop oldest WHOLE user turns until the history fits the budget.

    Never drops mid-turn (tool_call/result pairs stay intact by construction)
    and never drops the latest turn — an over-budget single turn passes
    through rather than producing an empty request.
    """
    if _history_len(history) <= max_chars or len(user_turn_boundaries) <= 1:
        return history
    # boundaries are positions in `history`; find the smallest start that
    # fits, keeping at least the last turn.
    for start in user_turn_boundaries[1:]:
        if _history_len(history[start:]) <= max_chars:
            return history[start:]
    return history[user_turn_boundaries[-1]:]


def _truncate_result(result: Any) -> str:
    text = json.dumps(result, ensure_ascii=False, default=str)
    if len(text) > TOOL_RESULT_TRUNCATE:
        return text[:TOOL_RESULT_TRUNCATE] + TRUNCATE_NOTE
    return text


def derive_chat_rows(events: list[SessionEvent]) -> list[dict[str, Any]]:
    """Project UI chat rows (the legacy agent_messages shape) from the log.

    Each tool/call → row without result; the paired tool/result attaches the
    result to that row (matched by call_id)."""
    rows: list[dict[str, Any]] = []
    row_by_call_id: dict[str, dict[str, Any]] = {}
    for e in events:
        d = e.data
        if e.type == USER_MESSAGE:
            row = {"role": "user", "content": d.get("content", "")}
            if d.get("mentions"):
                row["mentions"] = d["mentions"]
            if d.get("attachments"):
                row["attachments"] = d["attachments"]
            rows.append(row)
        elif e.type == STEERING_MESSAGE:
            row = {"role": "user", "content": d.get("content", ""), "status": "steering"}
            if d.get("mentions"):
                row["mentions"] = d["mentions"]
            if d.get("attachments"):
                row["attachments"] = d["attachments"]
            rows.append(row)
        elif e.type == TOOL_CALL:
            fn_name = d.get("tool_name") or "tool"
            row: dict[str, Any] = {
                "role": "tool",
                "agent_name": d.get("agent_name"),
                "tool_name": fn_name,
                "tool_args": _parse_args(d.get("arguments")),
                "tool_result": None,
                "content": "",
            }
            rows.append(row)
            call_id = d.get("call_id")
            if call_id:
                row_by_call_id[call_id] = row
        elif e.type == ASSISTANT_MESSAGE:
            name = d.get("agent_name")
            content = (d.get("content") or "").strip()
            if content:
                row: dict[str, Any] = {
                    "role": "assistant",
                    "agent_name": name,
                    "content": content,
                    "status": d.get("status"),
                }
                reasoning = d.get("reasoning")
                if reasoning:
                    row["reasoning"] = reasoning
                rows.append(row)
        elif e.type == TOOL_RESULT:
            row = row_by_call_id.get(d.get("call_id", ""))
            if row is not None:
                row["tool_result"] = d.get("result")
    return rows


def _parse_args(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        return None


def derive_plan(events: list[SessionEvent]) -> dict[str, Any] | None:
    """Project the latest swarm plan + per-task status from the event log.

    The most recent `plan/created` event resets the task list (each turn may
    produce a new plan); `task/start` / `task/end` then annotate tasks by index.
    Returns None when no plan exists (simple/legacy turns).
    """
    tasks: list[dict[str, Any]] = []
    note = ""
    statuses: dict[int, str] = {}
    for e in events:
        d = e.data
        if e.type == PLAN_CREATED:
            tasks = []
            statuses = {}
            note = d.get("note") or ""
            for t in d.get("tasks") or []:
                if not isinstance(t, dict):
                    continue
                tasks.append(
                    {
                        "role": t.get("role") or "script",
                        "goal": (t.get("goal") or "").strip(),
                        "status": "pending",
                    }
                )
        elif e.type == TASK_START:
            idx = d.get("index")
            if isinstance(idx, int) and 0 <= idx < len(tasks):
                statuses[idx] = "running"
        elif e.type == TASK_END:
            idx = d.get("index")
            if isinstance(idx, int) and 0 <= idx < len(tasks):
                statuses[idx] = d.get("status") or "done"
    if not tasks:
        return None
    for i, t in enumerate(tasks):
        if i in statuses:
            t["status"] = statuses[i]
    return {"tasks": tasks, "note": note}


# ─────────────────────────────────────────────────────────────────────────
# Legacy backfill: agent_messages → events
# ─────────────────────────────────────────────────────────────────────────


def backfill_from_messages(session_id: int) -> bool:
    """Import a legacy agent_messages trail into the event log (once).

    Returns True when events were appended (first import), False when the
    session already had events (or had no messages).
    """
    conn = _db()
    try:
        has_events = conn.execute(
            "SELECT 1 FROM agent_events WHERE session_id = ? LIMIT 1", (session_id,)
        ).fetchone()
        if has_events:
            return False
        rows = conn.execute(
            "SELECT * FROM agent_messages WHERE session_id = ? ORDER BY id", (session_id,)
        ).fetchall()
        if not rows:
            return False
        events: list[tuple[str, dict[str, Any]]] = []
        for r in rows:
            m = row_to_dict(r)
            role = m["role"]
            if role == "user":
                events.append((USER_MESSAGE, {"content": m["content"]}))
            elif role == "assistant":
                events.append(
                    (
                        ASSISTANT_MESSAGE,
                        {
                            "content": m["content"],
                            "agent_name": m.get("agent_name"),
                            "status": m.get("status"),
                        },
                    )
                )
            elif role == "tool":
                events.append(
                    (
                        TOOL_CALL,
                        {
                            "call_id": f"legacy_{m['id']}",
                            "tool_name": m.get("tool_name"),
                            "arguments": m.get("tool_args_json"),
                        },
                    )
                )
                result = None
                if m.get("tool_result_json"):
                    try:
                        result = json.loads(m["tool_result_json"])
                    except json.JSONDecodeError:
                        result = {"raw": m["tool_result_json"]}
                events.append(
                    (
                        TOOL_RESULT,
                        {
                            "call_id": f"legacy_{m['id']}",
                            "tool_name": m.get("tool_name"),
                            "result": result,
                        },
                    )
                )
        cur_count = 0
        for event_type, data in events:
            cur_count += 1
            conn.execute(
                "INSERT INTO agent_events (session_id, seq, type, data_json) VALUES (?, ?, ?, ?)",
                (session_id, cur_count, event_type, json.dumps(data, ensure_ascii=False, default=str)),
            )
        conn.execute(
            "UPDATE agent_sessions SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (session_id,),
        )
        conn.commit()
        return cur_count > 0
    finally:
        conn.close()
