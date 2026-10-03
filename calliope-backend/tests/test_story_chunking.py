"""Chunked story-beat generation: a long target (50 beats) is drafted in
several modest LLM calls (brief + continuations) instead of one giant one."""
from __future__ import annotations

import pytest


def _beats(n: int, start: int = 1):
    return [
        {
            "order_index": start + i,
            "title": f"B{start + i}",
            "description": f"beat {start + i} happens",
        }
        for i in range(n)
    ]


@pytest.fixture
def fake_llm(monkeypatch):
    """Fake generate_structured that reads THIS CHUNK / order_index range
    from the user prompt and returns exactly that many beats."""
    calls: list[dict] = []

    async def fake(messages, temperature=0.7):
        user = messages[1]["content"]
        chunk_n = 0
        start = 1
        for line in user.splitlines():
            if "THIS CHUNK:" in line:
                # "exactly N scenes/beats, order_index A through B"
                import re

                m = re.search(r"exactly (\d+) beats", line)
                if m:
                    chunk_n = int(m.group(1))
                m2 = re.search(r"order_index (\d+) through (\d+)", line)
                if m2:
                    start = int(m2.group(1))
        # Single-call path: "beats MUST contain EXACTLY {beat_n} objects"
        if chunk_n == 0:
            import re as _re

            m = _re.search(r"EXACTLY (\d+) objects", user)
            if m:
                chunk_n = int(m.group(1))
        is_brief = "OUTPUT SCHEMA" in user and "characters" in user
        calls.append({"start": start, "n": chunk_n, "brief": is_brief})
        out = {"beats": _beats(chunk_n, start)}
        if is_brief:
            out = {
                "title": "Chunked Tale",
                "logline": "A long story told in chunks.",
                "characters": [{"name": "Ada", "role": "protagonist", "age": "30",
                                 "appearance": "tall", "personality": "brave"}],
                "locations": [{"name": "Tower", "description": "stone"}],
                "items": [{"name": "Key", "description": "brass"}],
                **out,
            }
        return out

    monkeypatch.setattr("calliope.routers.story.generate_structured", fake)
    return calls


def test_long_story_splits_into_chunks(client, fake_llm):
    # 10 minutes -> 50 beats -> brief(12) + 12 + 12 + 12 + 2
    r = client.post(
        "/api/projects",
        json={"title": "Long", "idea": "epic", "target_duration": "10 minutes"},
    )
    pid = r.json()["id"]
    resp = client.post(f"/api/projects/{pid}/generate-story")
    assert resp.status_code == 200, resp.text

    story = client.get(f"/api/projects/{pid}/story").json()
    beats = story["beats"]
    assert len(beats) == 50
    # order_index is a gapless 1..50 despite per-chunk numbering
    assert [b["order_index"] for b in beats] == list(range(1, 51))
    # brief call first, then continuations
    assert fake_llm[0]["brief"] is True
    assert all(not c["brief"] for c in fake_llm[1:])
    # cast/locations/items came from the brief call and persisted once
    assert len(story["characters"]) == 1
    assert len(story["locations"]) == 1
    assert len(story["items"]) == 1


def test_short_story_stays_single_call(client, fake_llm):
    r = client.post(
        "/api/projects",
        json={"title": "Short", "idea": "tiny", "target_duration": "30 seconds"},
    )
    pid = r.json()["id"]
    assert client.post(f"/api/projects/{pid}/generate-story").status_code == 200
    story = client.get(f"/api/projects/{pid}/story").json()
    assert len(story["beats"]) == 4
    # one call only (4 beats <= STORY_CHUNK)
    assert len(fake_llm) == 1


def test_generated_counts_every_table_it_wrote(client, fake_llm):
    """The response must name each table, not just beats.

    The agent reads this dict as its only evidence of what exists. Reporting
    `{"beats": 50}` alone made a story agent conclude the cast had not been
    created and stop without adding it — on a run where the LLM had actually
    returned and the router had actually inserted 4 characters, 4 locations and
    8 items (observed 2026-10-03, project 3).
    """
    r = client.post(
        "/api/projects",
        json={"title": "Counted", "idea": "epic", "target_duration": "10 minutes"},
    )
    pid = r.json()["id"]
    resp = client.post(f"/api/projects/{pid}/generate-story")
    assert resp.status_code == 200, resp.text
    generated = resp.json()["generated"]
    assert generated == {
        "beats": 50,
        "characters": 1,
        "locations": 1,
        "items": 1,
    }
    # And the counts agree with what is actually in the DB.
    story = client.get(f"/api/projects/{pid}/story").json()
    assert len(story["beats"]) == generated["beats"]
    assert len(story["characters"]) == generated["characters"]
    assert len(story["locations"]) == generated["locations"]
    assert len(story["items"]) == generated["items"]


def test_missing_cast_is_reported_as_zero_not_omitted(client, monkeypatch):
    """A brief with no cast must say `characters: 0`, so the agent can act on it.

    Silently omitting the key is what made the shortfall invisible: the agent
    could not tell "the cast came back empty, go add it" from "the tool does
    not manage the cast".
    """
    import calliope.routers.story as story_router

    async def fake(messages, temperature=0.7):
        user = messages[1]["content"]
        import re

        m = re.search(r"exactly (\d+) beats", user)
        chunk_n = int(m.group(1)) if m else 4
        m2 = re.search(r"order_index (\d+) through (\d+)", user)
        start = int(m2.group(1)) if m2 else 1
        is_brief = "OUTPUT SCHEMA" in user and "characters" in user
        out = {"beats": _beats(chunk_n, start)}
        if is_brief:
            out = {
                "title": "Castless",
                "logline": "No cast came back.",
                "characters": [],
                "locations": [],
                "items": [],
                **out,
            }
        return out

    monkeypatch.setattr(story_router, "generate_structured", fake)

    r = client.post(
        "/api/projects",
        json={"title": "Castless", "idea": "epic", "target_duration": "10 minutes"},
    )
    pid = r.json()["id"]
    resp = client.post(f"/api/projects/{pid}/generate-story")
    assert resp.status_code == 200, resp.text
    generated = resp.json()["generated"]
    assert generated["beats"] == 50
    assert generated["characters"] == 0
    assert generated["locations"] == 0
    assert generated["items"] == 0
