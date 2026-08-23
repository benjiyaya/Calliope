from __future__ import annotations

import json

from calliope.comfyui.parser import parse_dynamic_inputs, parse_dynamic_outputs
from calliope.comfyui.patcher import patch_workflow
from calliope.config import settings
from calliope.db import get_db


SAMPLE_WORKFLOW = {
    "102": {
        "inputs": {"value": 1280},
        "class_type": "PrimitiveInt",
        "_meta": {"title": "Width (Input:width)"},
    },
    "209": {
        "inputs": {"value": "My prompt"},
        "class_type": "PrimitiveStringMultiline",
        "_meta": {"title": "Text Prompt (Input:prompt)"},
    },
    "10": {
        "inputs": {"image": "x.png"},
        "class_type": "LoadImage",
        "_meta": {"title": "Reference Image (Input:character)"},
    },
    "99": {
        "inputs": {"filename_prefix": "out"},
        "class_type": "SaveImage",
        "_meta": {"title": "Final Image (Output:image)"},
    },
}


def test_parse_dynamic_inputs_outputs():
    inputs = parse_dynamic_inputs(SAMPLE_WORKFLOW)
    outputs = parse_dynamic_outputs(SAMPLE_WORKFLOW)
    assert len(inputs) == 3
    assert {i["kind"] for i in inputs} >= {"number", "textarea", "image"}
    by_role = {i["role"]: i for i in inputs}
    assert by_role["width"]["nodeId"] == "102"
    assert by_role["prompt"]["nodeId"] == "209"
    assert by_role["character"]["nodeId"] == "10"
    assert len(outputs) == 1
    assert outputs[0]["kind"] == "image"
    assert outputs[0]["role"] == "image"


def test_patch_workflow_by_node_id():
    patched = patch_workflow(SAMPLE_WORKFLOW, {"209": "New prompt", "102": 720, "10": "ref.png"})
    assert patched["209"]["inputs"]["value"] == "New prompt"
    assert patched["102"]["inputs"]["value"] == 720
    assert patched["10"]["inputs"]["image"] == "ref.png"


def test_workflow_analyze_and_create(client):
    r = client.post("/api/workflows/analyze", json={"workflow_json": SAMPLE_WORKFLOW})
    assert r.status_code == 200
    assert len(r.json()["inputs"]) == 3

    r = client.post(
        "/api/workflows",
        json={"name": "Test WF", "kind": "image", "workflow_json": SAMPLE_WORKFLOW},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["name"] == "Test WF"
    assert len(data["input_schema"]) == 3


def test_story_replace_on_regenerate(client, monkeypatch):
    async def fake_structured(messages, temperature=0.7):
        return {
            "title": "T",
            "logline": "L",
            "beats": [
                {"order_index": i, "title": f"B{i}", "description": "d"} for i in range(1, 5)
            ],
            "characters": [
                {
                    "name": "Hero",
                    "role": "protagonist",
                    "age": "20",
                    "appearance": "tall",
                    "personality": "brave",
                }
            ],
            "locations": [{"name": "Forest", "description": "dark woods"}],
        }

    monkeypatch.setattr("calliope.routers.story.generate_structured", fake_structured)

    r = client.post("/api/projects", json={"title": "P", "idea": "idea"})
    pid = r.json()["id"]
    r = client.post(f"/api/projects/{pid}/generate-story")
    assert r.status_code == 200
    r = client.get(f"/api/projects/{pid}/story")
    assert len(r.json()["beats"]) == 4
    assert len(r.json()["characters"]) == 1

    # regenerate should replace, not append
    r = client.post(f"/api/projects/{pid}/generate-story")
    assert r.status_code == 200
    r = client.get(f"/api/projects/{pid}/story")
    assert len(r.json()["beats"]) == 4
    assert len(r.json()["characters"]) == 1


def test_story_replace_clears_scene_locations(client, monkeypatch):
    """replace=true deletes all locations; scenes.location_id (no FK) must be
    nulled, not left dangling at dead location rows."""
    async def fake_structured(messages, temperature=0.7):
        return {
            "title": "T",
            "logline": "L",
            "beats": [
                {"order_index": i, "title": f"B{i}", "description": "d"} for i in range(1, 5)
            ],
            "characters": [],
            "locations": [{"name": "Forest", "description": "dark woods"}],
        }

    monkeypatch.setattr("calliope.routers.story.generate_structured", fake_structured)

    r = client.post("/api/projects", json={"title": "P2", "idea": "idea"})
    pid = r.json()["id"]
    assert client.post(f"/api/projects/{pid}/generate-story").status_code == 200

    loc = client.get(f"/api/projects/{pid}/story").json()["locations"][0]["id"]
    scene = client.post(
        f"/api/projects/{pid}/scenes",
        json={"order_index": 1, "heading": "S1", "location_id": loc},
    ).json()
    assert scene["location_id"] == loc

    # Regenerate with replace: locations wiped → scene must not dangle.
    assert client.post(f"/api/projects/{pid}/generate-story").status_code == 200

    conn = get_db(settings.db_path)
    try:
        row = conn.execute(
            "SELECT location_id FROM scenes WHERE id = ?", (scene["id"],)
        ).fetchone()
        assert row["location_id"] is None
        # And the new story has its own fresh location row.
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM locations WHERE project_id = ?", (pid,)
        ).fetchone()["n"]
        assert n == 1
    finally:
        conn.close()


def test_story_seeds_items(client, monkeypatch):
    async def fake_structured(messages, temperature=0.7):
        return {
            "title": "T",
            "logline": "L",
            "beats": [
                {"order_index": i, "title": f"B{i}", "description": "d"} for i in range(1, 5)
            ],
            "characters": [],
            "locations": [],
            "items": [
                {"name": "Magic Box", "description": "a glowing rune-carved chest"},
                {"name": "Sword", "description": "a chipped steel blade"},
            ],
        }

    monkeypatch.setattr("calliope.routers.story.generate_structured", fake_structured)

    r = client.post("/api/projects", json={"title": "P3", "idea": "idea"})
    pid = r.json()["id"]
    assert client.post(f"/api/projects/{pid}/generate-story").status_code == 200
    data = client.get(f"/api/projects/{pid}/story").json()
    assert len(data["items"]) == 2
    assert {i["name"] for i in data["items"]} == {"Magic Box", "Sword"}
    # seeded consistency_prompt mirrors the published item template
    assert "ITEM REFERENCE" in data["items"][0]["consistency_prompt"]

    # replace wipes and re-seeds items (no append)
    assert client.post(f"/api/projects/{pid}/generate-story").status_code == 200
    data = client.get(f"/api/projects/{pid}/story").json()
    assert len(data["items"]) == 2


def test_item_crud(client):
    r = client.post("/api/projects", json={"title": "P4"})
    pid = r.json()["id"]

    created = client.post(
        f"/api/projects/{pid}/items", json={"name": "Lantern", "description": "brass oil lamp"}
    ).json()
    assert created["name"] == "Lantern"

    updated = client.patch(
        f"/api/projects/{pid}/items/{created['id']}", json={"description": "battered brass lamp"}
    ).json()
    assert updated["description"] == "battered brass lamp"

    assert client.delete(f"/api/projects/{pid}/items/{created['id']}").json()["ok"] is True
    assert client.get(f"/api/projects/{pid}/assets").json()["items"] == []


def test_item_prompt_templates():
    from calliope.agent.prompts import item_image_prompt, item_reference_prompt

    item = {"name": "Key", "description": "an ornate iron key"}
    template = item_reference_prompt(item)
    assert template.startswith("ITEM REFERENCE — Key")
    assert "ornate iron key" in template
    assert item_image_prompt(item) == template

    saved = item_image_prompt({**item, "consistency_prompt": "custom prompt"})
    assert saved == "custom prompt"


def test_enqueue_dry_run_job(client):
    r = client.post("/api/projects", json={"title": "Q"})
    pid = r.json()["id"]
    r = client.post(
        "/api/workflows",
        json={"name": "Img", "kind": "image", "workflow_json": SAMPLE_WORKFLOW},
    )
    assert r.status_code == 200

    # create character manually via story path monkeypatch-less insert through generate-assets empty
    # use character create endpoint
    r = client.post(
        f"/api/projects/{pid}/characters",
        json={"name": "A", "appearance": "blue hair"},
    )
    assert r.status_code == 200

    r = client.post(f"/api/projects/{pid}/generate-assets", json={"missing_only": True})
    assert r.status_code == 200
    jobs = r.json()["jobs"]
    assert len(jobs) >= 1

    # wait briefly for dry-run worker
    import time

    done = False
    for _ in range(20):
        time.sleep(0.25)
        listed = client.get(f"/api/jobs?project_id={pid}").json()
        if listed and listed[0]["status"] in {"done", "failed"}:
            done = listed[0]["status"] == "done"
            break
    assert done


def test_settings_dry_run(client):
    r = client.get("/api/settings")
    assert r.status_code == 200
    assert "dry_run" in r.json()
    r = client.post("/api/settings", json={"dry_run": True})
    assert r.status_code == 200
    assert r.json()["dry_run"] is True


def test_duration_beat_budget():
    from calliope.agent.prompts import (
        estimate_target_seconds,
        recommend_beat_count,
        story_generation_user_prompt,
    )

    assert estimate_target_seconds("2 minutes") == 120
    assert recommend_beat_count("2 minutes") == 10
    assert recommend_beat_count("medium (~2 min)") == 10
    assert recommend_beat_count("10 minutes") == 50
    assert recommend_beat_count("30 seconds") == 4
    prompt = story_generation_user_prompt("T", "idea", "Horror", "Dark", "10 minutes")
    assert "required_beat_count: 50" in prompt
    assert "EXACTLY 50" in prompt
