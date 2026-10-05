"""Batch H3 prompt compilation: fill the gaps before any render is queued.

The reason this exists is VRAM, not throughput. On a single GPU the LLM and the
video model cannot both be resident, so a batch that interleaves "rewrite the
prompt, then queue the render" asks llama.cpp for a model while H3 is loaded.
Compiling every prompt first keeps the LLM as the sole GPU tenant for the whole
compile pass; ComfyUI only starts once the last prompt exists.

These lock down the properties that actually matter:

* the drafts land, so the Generate that follows spends no LLM call;
* `only_missing` fills gaps without touching finished shots;
* `clip_ids=[one]` is a single-shot recompile;
* one dead endpoint costs ONE timeout for the batch, not one per clip;
* a clip that cannot be compiled is reported, not raised — the rest still lands.
"""
from __future__ import annotations

import asyncio
import json

from calliope.config import settings
from calliope.db import get_db

# ── fixtures / helpers ────────────────────────────────────────────────────────


def _mk_project(client, title: str) -> int:
    return client.post("/api/projects", json={"title": title, "tone": "noir"}).json()["id"]


def _add_scene(client, pid: int, order: int, **extra) -> dict:
    payload = {"order_index": order, "heading": f"S{order}", **extra}
    return client.post(f"/api/projects/{pid}/scenes", json=payload).json()


def _clip_id(pid: int, scene_id: int) -> int:
    conn = get_db(settings.db_path)
    try:
        row = conn.execute(
            "SELECT id FROM clips WHERE scene_id = ? AND project_id = ? "
            "ORDER BY order_index, id LIMIT 1",
            (scene_id, pid),
        ).fetchone()
        assert row is not None
        return int(row["id"])
    finally:
        conn.close()


def _insert_h3(conn, name: str) -> int:
    wf = {
        "10": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": ""},
            "_meta": {"title": "Main Prompt (Input:prompt)"},
        }
    }
    cur = conn.execute(
        """
        INSERT INTO workflows (name, kind, workflow_json, input_schema, output_schema,
                               prompt_profile, is_enabled)
        VALUES (?, 'video', ?, '[]', '[]', 'minimax_h3_ref', 1)
        """,
        (name, json.dumps(wf)),
    )
    conn.commit()
    return int(cur.lastrowid)


def _wire_h3(pid: int, clip_ids: list[int]) -> None:
    """Give every clip the H3 prompt profile — without it the fallback runs."""
    conn = get_db(settings.db_path)
    try:
        wf_id = _insert_h3(conn, "H3 batch")
        for cid in clip_ids:
            conn.execute("UPDATE clips SET workflow_id = ? WHERE id = ?", (wf_id, cid))
        conn.commit()
    finally:
        conn.close()


def _three_clips(client, pid: int) -> list[int]:
    ids = [_clip_id(pid, _add_scene(client, pid, i, action=f"Beat {i}.")["id"]) for i in (1, 2, 3)]
    _wire_h3(pid, ids)
    return ids


def _draft(clip_id: int) -> str | None:
    conn = get_db(settings.db_path)
    try:
        row = conn.execute(
            "SELECT video_settings_json FROM clips WHERE id = ?", (clip_id,)
        ).fetchone()
    finally:
        conn.close()
    return (json.loads(row["video_settings_json"] or "{}") or {}).get("prompt_draft")


def _stub_plan(clip_ids: list[int], monkeypatch) -> None:
    async def fake_plan(project_id, **kwargs):
        return {
            "based_on": "planhash",
            "overview": {"lighting": "sodium"},
            "requirements": {},
            "shots": [{"clip_id": c, "action": "beat", "lighting": "sodium"} for c in clip_ids],
            "refreshed": True,
        }

    monkeypatch.setattr("calliope.agent.video_agent.ensure_continuity_plan", fake_plan)


def _stub_rewrite(monkeypatch, body: str = "COMPILED") -> list[int]:
    """Count every rewrite so a breaker assertion can prove it stopped calling."""
    calls: list[int] = []

    async def fake_rewrite(self_, scene_, subjects, **kwargs):
        calls.append(1)
        return body

    monkeypatch.setattr("calliope.agent.video_agent._H3Compiler.rewrite", fake_rewrite)
    return calls


# ── tests ─────────────────────────────────────────────────────────────────────


def test_batch_compiles_every_clip_and_saves_the_drafts(client, monkeypatch):
    """The point of the whole exercise: prompt first, so Generate costs no LLM."""
    from calliope.agent.video_agent import rewrite_clip_prompts

    pid = _mk_project(client, "Batch all")
    ids = _three_clips(client, pid)
    _stub_plan(ids, monkeypatch)
    _stub_rewrite(monkeypatch)

    # force=True: the LLM compile pass this module tests (default is the instant
    # deterministic compile).
    out = asyncio.run(rewrite_clip_prompts(pid, only_missing=True, save=True, force=True))

    assert out["total"] == 3
    assert out["compiled"] == 3
    assert out["failed"] == 0
    assert out["skipped"] == 0
    assert out["endpoint_dead"] is False
    assert all(_draft(cid) == "COMPILED" for cid in ids)


def test_only_missing_leaves_finished_shots_alone(client, monkeypatch):
    """Filling gaps must not recompile a shot the operator already accepted."""
    from calliope.agent.video_agent import rewrite_clip_prompts

    pid = _mk_project(client, "Only missing")
    ids = _three_clips(client, pid)
    _stub_plan(ids, monkeypatch)
    _stub_rewrite(monkeypatch, body="FIRST PASS")

    asyncio.run(rewrite_clip_prompts(pid, only_missing=True, save=True, force=True))
    calls_after_first = len(_stub_rewrite(monkeypatch, body="SECOND PASS"))

    out = asyncio.run(rewrite_clip_prompts(pid, only_missing=True, save=True, force=True))

    assert out["skipped"] == 3
    assert out["compiled"] == 0
    assert calls_after_first == 0  # the second pass never reached the LLM
    assert all(_draft(cid) == "FIRST PASS" for cid in ids)


def test_clip_ids_recompiles_exactly_one_shot(client, monkeypatch):
    """The single-shot path is the same endpoint with a one-element list."""
    from calliope.agent.video_agent import rewrite_clip_prompts

    pid = _mk_project(client, "One shot")
    ids = _three_clips(client, pid)
    _stub_plan(ids, monkeypatch)
    calls = _stub_rewrite(monkeypatch, body="ONE SHOT")

    out = asyncio.run(rewrite_clip_prompts(pid, clip_ids=[ids[1]], force=True))

    assert out["total"] == 1
    assert out["compiled"] == 1
    assert len(calls) == 1
    assert _draft(ids[1]) == "ONE SHOT"
    assert _draft(ids[0]) is None and _draft(ids[2]) is None


def test_dead_endpoint_costs_one_timeout_not_one_per_clip(client, monkeypatch):
    """A dead LLM must not be waited on N times before anything renders.

    The failure is injected at the HTTP boundary, not by replacing `rewrite`,
    so the real breaker — the except branch that latches `self.dead` — is what
    is under test. Stubbing `rewrite` itself would skip that branch entirely.
    """
    from calliope.agent.video_agent import rewrite_clip_prompts

    pid = _mk_project(client, "Dead endpoint")
    ids = _three_clips(client, pid)
    _stub_plan(ids, monkeypatch)

    attempts: list[int] = []

    class _DeadClient:
        async def chat(self, *a, **kw):
            attempts.append(1)
            raise RuntimeError("connection refused")

        async def close(self):
            return None

    monkeypatch.setattr(
        "calliope.agent.video_agent.LLMClient.for_role",
        classmethod(lambda cls, role, **kw: _DeadClient()),
    )

    out = asyncio.run(rewrite_clip_prompts(pid, only_missing=True, force=True))

    assert len(attempts) == 1, "the breaker must stop the batch after the first failure"
    assert out["endpoint_dead"] is True
    # Every clip still gets a usable prompt — from the deterministic template.
    assert out["compiled"] == 3
    assert out["failed"] == 0
    assert all(_draft(cid) for cid in ids), "the fallback must still be saved"


def test_one_uncompilable_clip_does_not_lose_the_rest(client, monkeypatch):
    """Partial progress: a bad clip is reported, the batch still delivers."""
    from calliope.agent.video_agent import rewrite_clip_prompts

    pid = _mk_project(client, "Partial")
    ids = _three_clips(client, pid)
    _stub_plan(ids, monkeypatch)

    async def selective(self_, scene_, subjects, **kwargs):
        if "Beat 2" in (scene_.get("action") or ""):
            raise ValueError("shot 2 has no usable beat text")
        return "OK"

    monkeypatch.setattr("calliope.agent.video_agent._H3Compiler.rewrite", selective)

    out = asyncio.run(rewrite_clip_prompts(pid, force=True))

    assert out["failed"] == 1
    assert out["compiled"] == 2
    bad = [r for r in out["results"] if not r["ok"]]
    assert len(bad) == 1
    assert "no usable beat text" in bad[0]["error"]
    assert bad[0]["clip_id"] == ids[1]
    assert _draft(ids[1]) is None
    assert _draft(ids[0]) == "OK" and _draft(ids[2]) == "OK"


def test_save_false_leaves_nothing_behind(client, monkeypatch):
    """A dry run must not silently rewrite state the operator did not ask for."""
    from calliope.agent.video_agent import rewrite_clip_prompts

    pid = _mk_project(client, "Dry run")
    ids = _three_clips(client, pid)
    _stub_plan(ids, monkeypatch)
    _stub_rewrite(monkeypatch)

    out = asyncio.run(rewrite_clip_prompts(pid, only_missing=True, save=False, force=True))

    assert out["compiled"] == 3
    assert all(r["saved"] is False for r in out["results"])
    assert all(_draft(cid) is None for cid in ids)


def test_batch_endpoint_reports_per_clip_results(client, monkeypatch):
    """The UI contract: one entry per clip, addressed so it can be highlighted."""
    from calliope.agent.video_agent import rewrite_clip_prompts

    pid = _mk_project(client, "Shape")
    ids = _three_clips(client, pid)
    _stub_plan(ids, monkeypatch)
    _stub_rewrite(monkeypatch)

    out = asyncio.run(rewrite_clip_prompts(pid, force=True))

    assert [r["clip_id"] for r in out["results"]] == ids
    for r in out["results"]:
        assert set(r) >= {"clip_id", "label", "ok", "prompt", "profile", "from_draft", "saved"}
        assert r["ok"] is True
        assert r["label"].startswith("#")