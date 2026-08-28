"""Film export: stitch a project's scene clips into one mp4 via ffmpeg.

Pipeline (validated on real clips — do not reinvent): normalize each clip →
0.5s crossfades (xfade/acrossfade) → loudness normalize. ComfyUI is never
involved; export jobs run locally against files on disk.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import time
from pathlib import Path
from typing import Any

from calliope import config
from calliope.comfyui.dry_run import write_placeholder_mp4
from calliope.db import get_db, row_to_dict

logger = logging.getLogger("calliope.export")

# Normalization targets — every clip is conformed to these before stitching.
# The fps target is the clips' own majority fps (see target_fps), not a fixed
# rate: 24 fps generated clips must export at 24 fps, not be retimed to 30.
EXPORT_WIDTH = 1920
EXPORT_HEIGHT = 1080
DEFAULT_EXPORT_FPS = 30.0
XFADE_SEC = 0.5
PROGRESS_MIN_INTERVAL_SEC = 1.0

_running: dict[int, asyncio.subprocess.Process] = {}


def kill_running(job_id: int) -> bool:
    """Kill the ffmpeg process for a running export job. False when none."""
    proc = _running.get(job_id)
    if not proc or proc.returncode is not None:
        return False
    try:
        proc.kill()
    except ProcessLookupError:
        return False
    return True


def _require_binary(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise RuntimeError(f"{name} not found on PATH — install ffmpeg to enable film export.")
    return path


def slugify(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug or "film"


def parse_rate(rate: str | None) -> float:
    """ffprobe r_frame_rate ("24000/1001") as a float; 0.0 when unusable."""
    if not rate or "/" not in rate:
        return 0.0
    num, _, den = rate.partition("/")
    try:
        d = float(den)
        if d == 0:
            return 0.0
        return float(num) / d
    except ValueError:
        return 0.0


def target_fps(probes: list[dict[str, Any]]) -> float:
    """Majority rounded fps across clips; tie → lower; none → default.

    xfade needs one common timebase, so a mixed 24/30 fps project is conformed
    to whichever rate most clips already use.
    """
    votes = [round(p["fps"]) for p in probes if p.get("fps")]
    if not votes:
        return DEFAULT_EXPORT_FPS
    counts: dict[int, int] = {}
    for v in votes:
        counts[v] = counts.get(v, 0) + 1
    best = max(counts.values())
    winners = sorted(v for v, c in counts.items() if c == best)
    return float(winners[0])


def collect_clips(project_id: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Scenes in story order, split into (with video_path, without)."""
    conn = get_db(config.settings.db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM scenes WHERE project_id = ? ORDER BY order_index",
            (project_id,),
        ).fetchall()
        scenes = [row_to_dict(r) for r in rows]
    finally:
        conn.close()
    with_clip = [s for s in scenes if s.get("video_path")]
    skipped = [s for s in scenes if not s.get("video_path")]
    return with_clip, skipped


def _project_title(project_id: int) -> str:
    conn = get_db(config.settings.db_path)
    try:
        row = conn.execute("SELECT title FROM projects WHERE id = ?", (project_id,)).fetchone()
        return row["title"] if row else f"project-{project_id}"
    finally:
        conn.close()


async def probe(path: str | Path) -> dict[str, Any]:
    """ffprobe a clip: duration (float seconds) and has_audio (bool)."""
    ffprobe = _require_binary("ffprobe")
    proc = await asyncio.create_subprocess_exec(
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "format=duration:stream=codec_type,r_frame_rate",
        "-of",
        "json",
        str(path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        tail = err.decode("utf-8", "replace")[-500:]
        raise RuntimeError(f"ffprobe failed for {path}: {tail}")
    data = json.loads(out.decode("utf-8", "replace") or "{}")
    duration = float((data.get("format") or {}).get("duration") or 0.0)
    streams = data.get("streams") or []
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    video_fps = 0.0
    for s in streams:
        if s.get("codec_type") == "video":
            video_fps = parse_rate(s.get("r_frame_rate"))
            break
    return {"duration": duration, "has_audio": has_audio, "fps": video_fps}


def build_ffmpeg_cmd(
    clips: list[dict[str, Any]],
    probes: list[dict[str, Any]],
    dest: str | Path,
    *,
    fps: float | None = None,
) -> list[str]:
    """Assemble the validated normalize → xfade → loudnorm ffmpeg command.

    fps=None conforms every clip to target_fps(probes) — the clips' own
    majority rate. Pass an explicit fps to override the vote.
    """
    if not clips:
        raise RuntimeError("No clips to export")
    if len(clips) != len(probes):
        raise ValueError("clips and probes must align")
    rate = float(fps) if fps else target_fps(probes)
    rate_str = f"{rate:.6g}"

    n = len(clips)
    ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
    cmd: list[str] = [ffmpeg, "-nostdin", "-nostats", "-progress", "pipe:1", "-y"]
    for clip in clips:
        cmd += ["-i", str(clip["video_path"])]
    # Silent clips get a lavfi silence donor input, appended after the clip inputs
    # so input indices for real clips stay 0..n-1.
    lavfi_index: dict[int, int] = {}
    next_index = n
    for i, p in enumerate(probes):
        if not p["has_audio"]:
            lavfi_index[i] = next_index
            next_index += 1
            cmd += ["-f", "lavfi", "-i", "anullsrc=cl=stereo:r=48000"]

    v_chain = (
        f"scale={EXPORT_WIDTH}:{EXPORT_HEIGHT}:force_original_aspect_ratio=decrease,"
        f"pad={EXPORT_WIDTH}:{EXPORT_HEIGHT}:(ow-iw)/2:(oh-ih)/2,"
        f"setsar=1,fps={rate_str},settb=AVTB"
    )
    a_chain = "aformat=sample_rates=48000:channel_layouts=stereo"

    filters: list[str] = []
    for i, p in enumerate(probes):
        filters.append(f"[{i}:v]{v_chain}[v{i}]")
        if p["has_audio"]:
            filters.append(f"[{i}:a]{a_chain}[a{i}]")
        else:
            filters.append(
                f"[{lavfi_index[i]}:a]atrim=duration={float(p['duration']):.3f},"
                f"asetpts=PTS-STARTPTS,{a_chain}[a{i}]"
            )

    durations = [float(p["duration"]) for p in probes]
    if n == 1:
        # Single clip: no crossfade, but same normalize + loudness treatment.
        filters.append("[v0]format=yuv420p[vout]")
        filters.append("[a0]loudnorm=I=-16:TP=-1.5:LRA=11[aout]")
    else:
        for k in range(1, n):
            offset = sum(durations[:k]) - k * XFADE_SEC
            vin = "[v0]" if k == 1 else f"[x{k - 1}]"
            ain = "[a0]" if k == 1 else f"[c{k - 1}]"
            filters.append(
                f"{vin}[v{k}]xfade=transition=fade:duration={XFADE_SEC}:offset={offset:.3f}[x{k}]"
            )
            filters.append(f"{ain}[a{k}]acrossfade=d={XFADE_SEC}:c1=tri:c2=tri[c{k}]")
        # format=yuv420p AFTER the final xfade — the chain renegotiates to
        # yuv444p otherwise and players choke on the result.
        filters.append(f"[x{n - 1}]format=yuv420p[vout]")
        filters.append(f"[c{n - 1}]loudnorm=I=-16:TP=-1.5:LRA=11[aout]")

    cmd += ["-filter_complex", ";".join(filters)]
    cmd += ["-map", "[vout]", "-map", "[aout]"]
    cmd += [
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "20",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-movflags",
        "+faststart",
        str(dest),
    ]
    return cmd


async def run_export(
    job: dict[str, Any],
    payload: dict[str, Any],
    bus: Any,
    *,
    dry_run: bool = False,
) -> list[str]:
    """Run one export job; returns the output mp4 path list."""
    project_id = job["project_id"]
    job_id = job["id"]
    clips, skipped = collect_clips(project_id)
    if not clips:
        raise RuntimeError("No scene clips to export — generate videos first")

    for scene in skipped:
        heading = (scene.get("heading") or f"Scene {scene.get('order_index')}").strip()
        await bus.publish(
            "job.progress",
            {
                "job_id": job_id,
                "kind": "export",
                "message": f"Skipping scene «{heading}» — no clip",
            },
        )

    dest_dir = Path(config.settings.assets_dir) / str(project_id) / "export"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{slugify(_project_title(project_id))}-job{job_id}.mp4"

    if dry_run:
        write_placeholder_mp4(dest, label=f"job-{job_id}-export")
        return [str(dest)]

    _require_binary("ffmpeg")
    probes = [await probe(clip["video_path"]) for clip in clips]
    fps = target_fps(probes)
    durations = [float(p["duration"]) for p in probes]
    total_us = (sum(durations) - XFADE_SEC * max(0, len(clips) - 1)) * 1_000_000

    cmd = build_ffmpeg_cmd(clips, probes, dest, fps=fps)
    logger.info("Export job %s: %s", job_id, " ".join(cmd))

    await bus.publish(
        "job.progress",
        {"job_id": job_id, "kind": "export", "percent": 0.0, "message": f"Exporting film ({fps:.6g} fps)…"},
    )
    last_publish = time.monotonic()

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _running[job_id] = proc
    try:
        assert proc.stdout is not None and proc.stderr is not None
        # Drain stderr concurrently — a full pipe buffer would deadlock ffmpeg.
        stderr_task = asyncio.create_task(proc.stderr.read())
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            text = line.decode("utf-8", "replace").strip()
            if not text.startswith("out_time_us="):
                continue
            try:
                out_us = int(text.split("=", 1)[1])
            except ValueError:
                continue
            now = time.monotonic()
            if now - last_publish < PROGRESS_MIN_INTERVAL_SEC:
                continue
            last_publish = now
            percent = min(99.0, max(0.0, out_us / total_us * 100.0)) if total_us > 0 else 0.0
            await bus.publish(
                "job.progress",
                {
                    "job_id": job_id,
                    "kind": "export",
                    "percent": round(percent, 1),
                    "message": "Exporting film…",
                },
            )
        stderr_bytes = await stderr_task
        returncode = await proc.wait()
    finally:
        _running.pop(job_id, None)

    if returncode != 0:
        tail = stderr_bytes.decode("utf-8", "replace")[-2000:]
        raise RuntimeError(f"ffmpeg export failed (exit {returncode}): {tail}")
    return [str(dest)]
