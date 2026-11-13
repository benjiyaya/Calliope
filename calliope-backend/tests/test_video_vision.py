"""Video-attachment vision tests: attachments project as extracted frames.

These run against the shared temp-scoped `client` fixture (never the real
DB / assets dir). `skipif` covers machines without ffmpeg/ffprobe on PATH.
"""
from __future__ import annotations

import shutil
import subprocess

import pytest

from calliope.config import settings
from calliope.agent.media import (
    _MAX_VIDEO_FRAMES,
    _video_attachment_frames,
    project_user_content,
)

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not on PATH",
)


def _make_test_clip(duration: float = 2.0) -> str:
    """Tiny 2s MP4 (color sweep) written under the temp assets dir."""
    settings.assets_dir.mkdir(parents=True, exist_ok=True)
    path = settings.assets_dir / "vision-test-clip.mp4"
    cmd = [
        shutil.which("ffmpeg"), "-y", "-v", "error",
        "-f", "lavfi", "-i", f"testsrc=duration={duration}:size=320x240:rate=10",
        "-pix_fmt", "yuv420p", str(path),
    ]
    subprocess.run(cmd, check=True, capture_output=True, timeout=60)
    return str(path)


def test_video_attachment_becomes_frame_parts(client):
    clip = _make_test_clip()
    parts = project_user_content("recreate this fight", attachments=[{"path": clip, "kind": "video", "name": "clip.mp4"}])
    assert isinstance(parts, list)
    text = parts[0]
    assert text["type"] == "text"
    assert "recreate this fight" in text["text"]
    assert "[Video frames in order]" in text["text"]
    assert "frame at" in text["text"]
    image_parts = [p for p in parts if p.get("type") == "image_url"]
    assert 1 <= len(image_parts) <= _MAX_VIDEO_FRAMES
    for p in image_parts:
        assert p["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_frame_timestamps_are_ordered_and_bounded(client):
    clip = _make_test_clip(duration=4.0)
    frames = _video_attachment_frames(clip)
    assert frames, "expected extracted frames"
    assert len(frames) <= _MAX_VIDEO_FRAMES
    stamps = [ts for ts, _ in frames]
    assert stamps == sorted(stamps)
    assert all(0 <= ts <= 4.0 for ts in stamps)


def test_missing_video_file_degrades_to_text(client):
    parts = project_user_content(
        "look",
        attachments=[{"path": str(settings.assets_dir / "nope.mp4"), "kind": "video"}],
    )
    assert isinstance(parts, str)
    assert "look" in parts


def test_outside_assets_dir_video_is_ignored(client):
    parts = project_user_content(
        "look",
        attachments=[{"path": "C:/Windows/notepad.exe", "kind": "video"}],
    )
    assert isinstance(parts, str)


def test_audio_kind_stays_text_only(client):
    parts = project_user_content(
        "listen",
        attachments=[{"path": str(settings.assets_dir / "song.mp3"), "kind": "audio"}],
    )
    assert isinstance(parts, str)
