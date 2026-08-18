"""Script regenerate must honor an expanded scene board."""
from __future__ import annotations

from calliope.agent.prompts import build_script_messages, recommend_scene_count


def test_build_script_messages_honors_expanded_scene_count():
    msgs = build_script_messages(
        title="T",
        idea="idea",
        beats=[{"order_index": 1, "title": "B", "description": "d"}],
        characters=[{"id": 1, "name": "Hero", "role": "lead", "appearance": "tall"}],
        locations=[{"id": 1, "name": "Camp", "description": "night"}],
        target_duration="30 seconds",
        scene_count=6,
    )
    user = msgs[1]["content"]
    recommended = recommend_scene_count("30 seconds")
    assert recommended <= 6
    assert "required_scene_count: 6" in user
    assert "EXACTLY 6 objects" in user
    assert "scenes.length == 6" in user


def test_script_regenerate_keeps_expanded_count(client, monkeypatch):
    calls: list[int] = []

    async def fake_structured(messages, temperature=0.7):
        user = messages[1]["content"]
        # Parse required count from prompt
        n = 4
        for line in user.splitlines():
            if line.startswith("required_scene_count:"):
                n = int(line.split(":", 1)[1].strip())
                break
        calls.append(n)
        return {
            "scenes": [
                {
                    "order_index": i,
                    "heading": f"SCENE {i}",
                    "action": f"action {i}",
                    "dialog": "",
                    "duration_sec": 5,
                    "character_ids": [],
                    "location_id": None,
                }
                for i in range(1, n + 1)
            ]
        }

    monkeypatch.setattr("calliope.agent.script_agent.generate_structured", fake_structured)

    r = client.post("/api/projects", json={"title": "Expand", "idea": "desert", "target_duration": "30 seconds"})
    pid = r.json()["id"]

    # Seed 4 scenes via generate
    r = client.post(f"/api/projects/{pid}/generate-script", json={"replace": True})
    assert r.status_code == 200
    assert len(r.json()["scenes"]) == calls[-1]

    # User expands board with empty scenes
    for i in range(2):
        existing = client.get(f"/api/projects/{pid}/scenes").json()["scenes"]
        client.post(
            f"/api/projects/{pid}/scenes",
            json={
                "order_index": len(existing) + 1,
                "heading": f"NEW SCENE {len(existing) + 1}",
                "action": "",
                "dialog": "",
                "duration_sec": 5,
            },
        )

    board = client.get(f"/api/projects/{pid}/scenes").json()["scenes"]
    expanded = len(board)
    assert expanded >= 6

    r = client.post(
        f"/api/projects/{pid}/generate-script",
        json={"replace": True, "scene_count": expanded},
    )
    assert r.status_code == 200
    assert len(r.json()["scenes"]) == expanded
    assert calls[-1] == expanded
