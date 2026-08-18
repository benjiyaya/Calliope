from __future__ import annotations

import json

from calliope.comfyui.parser import parse_dynamic_inputs, parse_dynamic_outputs
from calliope.comfyui.patcher import patch_workflow


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
