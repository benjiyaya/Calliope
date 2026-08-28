from __future__ import annotations

import time
from pathlib import Path

import pytest

from calliope.comfyui.client import ComfyUIClient
from calliope.export.runner import DEFAULT_EXPORT_FPS, build_ffmpeg_cmd, collect_clips, parse_rate, target_fps


def _make_project_with_clips(client, title: str = "My Film") -> int:
    pid = client.post("/api/projects", json={"title": title}).json()["id"]
    s1 = client.post(
        f"/api/projects/{pid}/scenes", json={"order_index": 1, "heading": "Opening"}
    ).json()
    s2 = client.post(
        f"/api/projects/{pid}/scenes", json={"order_index": 2, "heading": "Chase"}
    ).json()
    client.patch(
        f"/api/projects/{pid}/scenes/{s1['id']}", json={"video_path": "C:/clips/a.mp4"}
    )
    client.patch(
        f"/api/projects/{pid}/scenes/{s2['id']}", json={"video_path": "C:/clips/b.mp4"}
    )
    return pid


def test_build_ffmpeg_cmd_two_clips():
    clips = [{"video_path": "a.mp4"}, {"video_path": "b.mp4"}]
    probes = [
        {"duration": 8.02, "has_audio": True},
        {"duration": 8.0, "has_audio": True},
    ]
    cmd = build_ffmpeg_cmd(clips, probes, Path("out.mp4"))
    joined = " ".join(str(c) for c in cmd)
    # offset = 8.02 - 0.5 = 7.52
    assert "xfade=transition=fade:duration=0.5:offset=7.52" in joined
    assert "acrossfade=d=0.5:c1=tri:c2=tri" in joined
    # format=yuv420p must come AFTER the xfade output label
    assert "[x1]format=yuv420p[vout]" in joined
    assert "loudnorm=I=-16:TP=-1.5:LRA=11" in joined
    assert "scale=1920:1080:force_original_aspect_ratio=decrease" in joined
    assert "fps=30" in joined
    assert "-c:v libx264" in joined
    assert "-crf 20" in joined
    assert "-b:a 192k" in joined
    assert "-movflags +faststart" in joined
    assert "-progress pipe:1" in joined
    assert "anullsrc" not in joined
    assert cmd[-1] == "out.mp4"


def test_build_ffmpeg_cmd_anullsrc_for_silent_clip():
    clips = [{"video_path": "a.mp4"}, {"video_path": "b.mp4"}]
    probes = [
        {"duration": 8.0, "has_audio": True},
        {"duration": 4.0, "has_audio": False},
    ]
    cmd = build_ffmpeg_cmd(clips, probes, "out.mp4")
    joined = " ".join(str(c) for c in cmd)
    # One lavfi silence donor input appended after the 2 clip inputs (index 2)
    assert joined.count("-f lavfi -i anullsrc=cl=stereo:r=48000") == 1
    assert (
        "[2:a]atrim=duration=4.000,asetpts=PTS-STARTPTS,"
        "aformat=sample_rates=48000:channel_layouts=stereo[a1]" in joined
    )
    # Real clip keeps its own audio chain
    assert "[0:a]aformat=sample_rates=48000:channel_layouts=stereo[a0]" in joined
    assert "xfade=transition=fade:duration=0.5:offset=7.500" in joined


def test_build_ffmpeg_cmd_single_clip():
    cmd = build_ffmpeg_cmd(
        [{"video_path": "a.mp4"}], [{"duration": 5.0, "has_audio": True}], "out.mp4"
    )
    joined = " ".join(str(c) for c in cmd)
    assert "xfade" not in joined
    assert "acrossfade" not in joined
    assert "[v0]format=yuv420p[vout]" in joined
    assert "[a0]loudnorm=I=-16:TP=-1.5:LRA=11[aout]" in joined
    assert "-c:v libx264" in joined


def test_build_ffmpeg_cmd_no_clips_raises():
    with pytest.raises(RuntimeError):
        build_ffmpeg_cmd([], [], "out.mp4")


def test_collect_clips_split_and_order(client):
    pid = client.post("/api/projects", json={"title": "Order"}).json()["id"]
    s_b = client.post(
        f"/api/projects/{pid}/scenes", json={"order_index": 2, "heading": "B"}
    ).json()
    s_a = client.post(
        f"/api/projects/{pid}/scenes", json={"order_index": 1, "heading": "A"}
    ).json()
    client.post(f"/api/projects/{pid}/scenes", json={"order_index": 3, "heading": "C"})
    client.patch(f"/api/projects/{pid}/scenes/{s_b['id']}", json={"video_path": "b.mp4"})
    client.patch(f"/api/projects/{pid}/scenes/{s_a['id']}", json={"video_path": "a.mp4"})

    clips, skipped = collect_clips(pid)
    assert [c["heading"] for c in clips] == ["A", "B"]
    assert [s["heading"] for s in skipped] == ["C"]


def test_export_requires_scene_clips(client):
    pid = client.post("/api/projects", json={"title": "Empty"}).json()["id"]
    client.post(f"/api/projects/{pid}/scenes", json={"order_index": 1, "heading": "No clip"})
    r = client.post(f"/api/jobs/projects/{pid}/export")
    assert r.status_code == 400


def test_export_supersedes_pending(client):
    pid = _make_project_with_clips(client)
    try:
        client.post("/api/jobs/pause")
        r1 = client.post(f"/api/jobs/projects/{pid}/export")
        r2 = client.post(f"/api/jobs/projects/{pid}/export")
        assert r1.status_code == 200
        assert r2.status_code == 200
        j1 = client.get(f"/api/jobs/{r1.json()['job']['id']}").json()
        assert j1["status"] == "failed"
        assert "superseded" in j1["error"]
        j2 = client.get(f"/api/jobs/{r2.json()['job']['id']}").json()
        assert j2["status"] == "pending"
        assert j2["kind"] == "export"
    finally:
        client.post("/api/jobs/resume")


def test_export_job_dry_run(client):
    pid = _make_project_with_clips(client)
    r = client.post(f"/api/jobs/projects/{pid}/export")
    assert r.status_code == 200
    job = r.json()["job"]
    assert job["kind"] == "export"

    final = None
    for _ in range(40):
        time.sleep(0.25)
        got = client.get(f"/api/jobs/{job['id']}").json()
        if got["status"] in {"done", "failed"}:
            final = got
            break
    assert final is not None, "export job never finished"
    assert final["status"] == "done", final
    outputs = final["output_paths"]
    assert len(outputs) == 1
    norm = outputs[0].replace("\\", "/")
    assert f"/{pid}/export/" in norm
    assert norm.endswith(f"my-film-job{job['id']}.mp4")
    assert Path(outputs[0]).exists()

    # A finished export closes the project lifecycle
    project = client.get(f"/api/projects/{pid}").json()
    assert project["status"] == "completed"


def test_cancel_export_skips_comfy_interrupt(client, monkeypatch):
    calls = {"interrupt": 0, "kill": 0}

    async def fake_interrupt(self):
        calls["interrupt"] += 1

    def fake_kill(job_id: int) -> bool:
        calls["kill"] += 1
        return True

    monkeypatch.setattr(ComfyUIClient, "interrupt", fake_interrupt)
    monkeypatch.setattr("calliope.routers.jobs.kill_running", fake_kill)

    pid = _make_project_with_clips(client)
    try:
        client.post("/api/jobs/pause")
        job_id = client.post(f"/api/jobs/projects/{pid}/export").json()["job"]["id"]
        r = client.post(f"/api/jobs/{job_id}/cancel")
        assert r.status_code == 200
        assert calls["kill"] == 1
        assert calls["interrupt"] == 0
    finally:
        client.post("/api/jobs/resume")


def test_cancel_comfy_job_calls_interrupt(client, monkeypatch):
    calls = {"interrupt": 0, "kill": 0}

    async def fake_interrupt(self):
        calls["interrupt"] += 1

    def fake_kill(job_id: int) -> bool:
        calls["kill"] += 1
        return True

    monkeypatch.setattr(ComfyUIClient, "interrupt", fake_interrupt)
    monkeypatch.setattr("calliope.routers.jobs.kill_running", fake_kill)

    pid = client.post("/api/projects", json={"title": "Comfy"}).json()["id"]
    try:
        client.post("/api/jobs/pause")
        job_id = client.post(f"/api/jobs?project_id={pid}", json={"kind": "image"}).json()["id"]
        r = client.post(f"/api/jobs/{job_id}/cancel")
        assert r.status_code == 200
        assert calls["interrupt"] == 1
        assert calls["kill"] == 0
    finally:
        client.post("/api/jobs/resume")


def test_parse_rate():
    assert parse_rate("24/1") == 24.0
    assert abs(parse_rate("24000/1001") - 23.976023976) < 1e-6
    assert parse_rate("30000/1001") > 29.9
    assert parse_rate("0/0") == 0.0
    assert parse_rate("0/1") == 0.0
    assert parse_rate("garbage") == 0.0
    assert parse_rate(None) == 0.0
    assert parse_rate("") == 0.0


def test_target_fps_majority():
    probes = [{"fps": 24.0}, {"fps": 24.0}, {"fps": 30.0}]
    assert target_fps(probes) == 24.0


def test_target_fps_tie_prefers_lower():
    probes = [{"fps": 30.0}, {"fps": 24.0}]
    assert target_fps(probes) == 24.0


def test_target_fps_missing_falls_back_to_default():
    assert target_fps([]) == DEFAULT_EXPORT_FPS == 30.0
    assert target_fps([{"fps": 0.0}, {"fps": None}]) == 30.0
    assert target_fps([{"duration": 5.0, "has_audio": True}]) == 30.0


def test_build_ffmpeg_cmd_explicit_fps_24():
    clips = [{"video_path": "a.mp4"}]
    probes = [{"duration": 5.0, "has_audio": True, "fps": 30.0}]
    cmd = build_ffmpeg_cmd(clips, probes, "out.mp4", fps=24.0)
    joined = " ".join(str(c) for c in cmd)
    assert "fps=24" in joined
    assert "fps=30" not in joined


def test_build_ffmpeg_cmd_defaults_to_target_fps_vote():
    clips = [{"video_path": "a.mp4"}, {"video_path": "b.mp4"}]
    probes = [
        {"duration": 5.0, "has_audio": True, "fps": 24.0},
        {"duration": 5.0, "has_audio": True, "fps": 24.0},
    ]
    cmd = build_ffmpeg_cmd(clips, probes, "out.mp4")
    joined = " ".join(str(c) for c in cmd)
    assert "fps=24" in joined
    assert "fps=30" not in joined


def test_build_ffmpeg_cmd_fractional_fps_formatting():
    clips = [{"video_path": "a.mp4"}]
    probes = [{"duration": 5.0, "has_audio": True, "fps": 23.976023976}]
    # Explicit fractional rate: target_fps votes on rounded fps (24), so :.6g
    # formatting is only reachable via the explicit override.
    cmd = build_ffmpeg_cmd(clips, probes, "out.mp4", fps=23.976023976)
    joined = " ".join(str(c) for c in cmd)
    # :.6g keeps six significant digits — a valid ffmpeg fps value
    assert "fps=23.976" in joined
    assert "fps=23.976023976" not in joined
    assert "fps=24" not in joined
