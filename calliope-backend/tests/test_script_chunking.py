"""Chunked script generation: a big board is written in several modest LLM
calls (not one giant one that hangs), with progress events and per-chunk
renumbering."""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest

import calliope.agent.script_agent as sa
from calliope.agent.script_agent import _generate_scenes_chunked, generate_script


@pytest.fixture(autouse=True, scope="module")
def _never_touch_real_db():
    """Redirect data_dir before ANY write in this module (repo test rule)."""
    import calliope.config as config_module
    from calliope.db import migrate_db

    prev = {
        "data_dir": config_module.settings.data_dir,
        "assets_dir": config_module.settings.assets_dir,
        "dry_run": config_module.settings.dry_run,
    }
    with tempfile.TemporaryDirectory() as tmp:
        config_module.settings.data_dir = Path(tmp)
        config_module.settings.assets_dir = Path(tmp) / "assets"
        config_module.settings.dry_run = True
        asyncio.run(migrate_db(config_module.settings.db_path))
        yield
    for k, v in prev.items():
        setattr(config_module.settings, k, v)


def _scenes(n: int, start: int = 1):
    return {
        "scenes": [
            {
                "order_index": start + i,
                "heading": f"SCENE {start + i}",
                "action": f"action {start + i}",
                "dialog": "",
                "duration_sec": 5,
                "character_ids": [],
                "location_id": None,
            }
            for i in range(n)
        ]
    }


def test_big_board_splits_into_chunks(monkeypatch):
    calls: list[int] = []

    async def fake_structured(messages, temperature=0.7):
        user = messages[1]["content"]
        # THIS CHUNK: exactly N scenes
        n = 0
        for line in user.splitlines():
            if line.startswith("THIS CHUNK:"):
                n = int(line.split("exactly", 1)[1].split("scenes")[0].strip())
                break
        calls.append(n)
        return _scenes(n)

    monkeypatch.setattr(sa, "generate_structured", fake_structured)
    out = asyncio.run(
        _generate_scenes_chunked(
            project_id=1,
            p={"title": "T", "idea": "i", "target_duration": "2 minutes"},
            beats=[],
            characters=[],
            locations=[],
            required_scenes=9,
        )
    )
    # 9 scenes at SCRIPT_CHUNK=4 → 4+4+1
    assert calls == [4, 4, 1]
    assert len(out) == 9
    assert [s["order_index"] for s in out] == list(range(1, 10))


def test_small_board_single_call(monkeypatch):
    calls: list[int] = []

    async def fake_structured(messages, temperature=0.7):
        n = 3
        for line in messages[1]["content"].splitlines():
            if line.startswith("THIS CHUNK:"):
                n = int(line.split("exactly", 1)[1].split("scenes")[0].strip())
        calls.append(n)
        return _scenes(n)

    monkeypatch.setattr(sa, "generate_structured", fake_structured)
    out = asyncio.run(
        _generate_scenes_chunked(
            project_id=1,
            p={"title": "T", "idea": "i", "target_duration": "30 seconds"},
            beats=[],
            characters=[],
            locations=[],
            required_scenes=3,
        )
    )
    assert calls == [3]
    assert len(out) == 3


def test_restarts_order_index_is_renumbered(monkeypatch):
    """A model that restarts order_index at 1 for every chunk must still
    produce a 1..N board."""

    async def fake_structured(messages, temperature=0.7):
        # Always returns order_index 1..n regardless of the chunk requested
        n = 0
        for line in messages[1]["content"].splitlines():
            if line.startswith("THIS CHUNK:"):
                n = int(line.split("exactly", 1)[1].split("scenes")[0].strip())
        return _scenes(n, start=1)

    monkeypatch.setattr(sa, "generate_structured", fake_structured)
    out = asyncio.run(
        _generate_scenes_chunked(
            project_id=1,
            p={"title": "T", "idea": "i", "target_duration": "2 minutes"},
            beats=[],
            characters=[],
            locations=[],
            required_scenes=8,
        )
    )
    assert [s["order_index"] for s in out] == list(range(1, 9))


def test_short_chunk_retries_only_that_chunk(monkeypatch):
    calls: list[int] = []

    async def fake_structured(messages, temperature=0.7):
        n = 0
        for line in messages[1]["content"].splitlines():
            if line.startswith("THIS CHUNK:"):
                n = int(line.split("exactly", 1)[1].split("scenes")[0].strip())
        calls.append(n)
        # First attempt for the 4-scene chunk returns only 2; retry is full.
        if len(calls) == 1:
            return _scenes(2)
        return _scenes(n)

    monkeypatch.setattr(sa, "generate_structured", fake_structured)
    out = asyncio.run(
        _generate_scenes_chunked(
            project_id=1,
            p={"title": "T", "idea": "i", "target_duration": "1 minute"},
            beats=[],
            characters=[],
            locations=[],
            required_scenes=6,
        )
    )
    # chunk1 short (2) → retry chunk1 (4); then chunk2 (2)
    assert calls == [4, 4, 2]
    assert len(out) == 6


def test_continuity_tail_is_passed_to_next_chunk(monkeypatch):
    seen_tails: list[bool] = []

    async def fake_structured(messages, temperature=0.7):
        user = messages[1]["content"]
        seen_tails.append("Scenes already written" in user)
        n = 0
        for line in user.splitlines():
            if line.startswith("THIS CHUNK:"):
                n = int(line.split("exactly", 1)[1].split("scenes")[0].strip())
        return _scenes(n)

    monkeypatch.setattr(sa, "generate_structured", fake_structured)
    asyncio.run(
        _generate_scenes_chunked(
            project_id=1,
            p={"title": "T", "idea": "i", "target_duration": "1 minute"},
            beats=[],
            characters=[],
            locations=[],
            required_scenes=6,
        )
    )
    # First chunk has no tail; the second continues from the first's scenes.
    assert seen_tails == [False, True]


# ─────────────────────────────────────────────────────────────────────────
# Issue #64 — target runtime is a budget, not a floor
# ─────────────────────────────────────────────────────────────────────────


def test_chunk_prompt_carries_per_scene_budget():
    from calliope.agent.prompts import build_script_chunk_messages

    messages = build_script_chunk_messages(
        title="T",
        idea="i",
        beats=[],
        characters=[],
        locations=[],
        target_duration="30 seconds",
        scene_count=5,
        chunk_start=1,
        chunk_scenes=5,
    )
    user = messages[1]["content"]
    # 30s / 5 scenes = 6s per scene; both the total and the per-scene scale
    # must be stated so the model writes content volume to the runtime.
    assert "Per-scene budget: ~6 seconds" in user
    assert "must play in about 30s" in user


def test_script_prompt_carries_per_scene_budget():
    from calliope.agent.prompts import build_script_messages

    messages = build_script_messages(
        title="T",
        idea="i",
        beats=[],
        characters=[],
        locations=[],
        target_duration="30 seconds",
        scene_count=5,
    )
    user = messages[1]["content"]
    assert "Per-scene budget: ~6 seconds" in user
    assert "must play in about 30s" in user


def _mk_project() -> int:
    from calliope.db import get_db
    from calliope.config import settings

    conn = get_db(settings.db_path)
    try:
        cur = conn.execute(
            "INSERT INTO projects (title, idea, target_duration) VALUES ('Budget', 'i', '30 seconds')"
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def _oversized_scenes(n: int) -> dict:
    """Scenes whose content genuinely runs ~110s total (issue #64 numbers)."""
    long_dialog = "\n".join(f"SPEAKER: {'word ' * 40}" for _ in range(4))  # 160w ≈ 64s
    long_action = " ".join(["moves carefully forward"] * 40)  # 160w ≈ 19s
    return {
        "scenes": [
            {
                "order_index": i + 1,
                "heading": f"SCENE {i + 1}",
                "action": long_action,
                "dialog": long_dialog,
                "duration_sec": 8,
                "character_ids": [],
                "location_id": None,
            }
            for i in range(n)
        ]
    }


def test_oversized_script_durations_shrink_to_target(monkeypatch):
    """Issue #64 regression: a 30s target with ~110s of written content must
    rescale scene durations DOWN toward the target (old max(...,1.0) treated
    the target as a floor and left the overrun untouched — 1:51 vs 0:30)."""
    from calliope.config import settings
    from calliope.db import get_db

    async def fake_structured(messages, temperature=0.7):
        n = 0
        for line in messages[1]["content"].splitlines():
            if line.startswith("THIS CHUNK:"):
                n = int(line.split("exactly", 1)[1].split("scenes")[0].strip())
        return _oversized_scenes(n)

    monkeypatch.setattr(sa, "generate_structured", fake_structured)
    pid = _mk_project()
    asyncio.run(generate_script(pid, with_clips=False))

    conn = get_db(settings.db_path)
    try:
        rows = conn.execute(
            "SELECT duration_sec FROM scenes WHERE project_id = ? ORDER BY order_index",
            (pid,),
        ).fetchall()
    finally:
        conn.close()
    total = sum(r["duration_sec"] for r in rows)
    # 5 scenes × ~83s estimated ≈ 415s → floor 0.5 caps the shrink at ~207s.
    # The old code left 415s; the fix must pull meaningfully below it.
    assert total <= 220, f"scene durations did not shrink toward the 30s target: {total}s"
    assert all(r["duration_sec"] >= 3 for r in rows)
