"""Chunked script generation: a big board is written in several modest LLM
calls (not one giant one that hangs), with progress events and per-chunk
renumbering."""
from __future__ import annotations

import asyncio

import pytest

import calliope.agent.script_agent as sa
from calliope.agent.script_agent import _generate_scenes_chunked


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
