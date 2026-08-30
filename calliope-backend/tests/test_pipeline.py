from __future__ import annotations

import json
from pathlib import Path

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


def test_patch_workflow_vhs_audio_colon_widget():
    """VHS_LoadAudio's widget is literally `audio:` — the patcher must write it."""
    wf = {
        "5": {
            "inputs": {"audio:": "old.wav"},
            "class_type": "VHS_LoadAudio",
            "_meta": {"title": "Ref Audio (Input:audio)"},
        }
    }
    patched = patch_workflow(wf, {"5": "C:/assets/new.wav"})
    assert patched["5"]["inputs"]["audio:"] == "C:/assets/new.wav"
    # no bogus `audio` key gets added alongside
    assert "audio" not in patched["5"]["inputs"]


def test_patch_workflow_no_fuzzy_fallback():
    """Unknown field on a node with unrelated keys must not get rewritten."""
    wf = {
        "7": {
            "inputs": {"aspect_ratio": "16:9", "megapixels": 1.0},
            "class_type": "ResolutionSelector",
            "_meta": {"title": "Resolution (Input)"},
        }
    }
    patched = patch_workflow(wf, {"7": "9:16"})
    # computed field 'value' doesn't exist and has no sibling pair — stays put
    assert patched["7"]["inputs"].get("value") == "9:16"
    assert patched["7"]["inputs"]["aspect_ratio"] == "16:9"


def test_queue_prompt_surfaces_comfy_error_body(monkeypatch):
    """A 400 from Comfy /prompt must name the node, not just the status code."""
    import httpx
    from calliope.comfyui.client import ComfyUIClient

    body = {
        "error": {"type": "prompt_outputs_failed_validation"},
        "node_errors": {
            "12": {"errors": [{"message": "Invalid audio file: ref.m4a"}]},
        },
    }

    def fake_send(request, **kwargs):
        return httpx.Response(400, json=body, request=request)

    transport = httpx.MockTransport(fake_send)
    client = ComfyUIClient(base_url="http://comfy.test")
    client._http = httpx.AsyncClient(transport=transport)
    import asyncio

    async def run():
        try:
            await client.queue_prompt({"1": {"class_type": "X", "inputs": {}}})
        except RuntimeError as exc:
            return str(exc)
        finally:
            await client.close()

    msg = asyncio.run(run())
    assert "prompt_outputs_failed_validation" in msg
    assert "node 12" in msg
    assert "Invalid audio file" in msg


def test_upload_audio_flat_no_subfolder(tmp_path, monkeypatch):
    """Audio uploads go to the flat input dir and return a bare filename."""
    import asyncio
    import httpx
    from calliope.comfyui.client import ComfyUIClient

    sent: dict = {}

    def fake_send(request, **kwargs):
        sent["url"] = str(request.url)
        sent["content_type"] = request.headers.get("content-type", "")
        return httpx.Response(200, json={"name": "ref.wav", "subfolder": ""}, request=request)

    transport = httpx.MockTransport(fake_send)
    client = ComfyUIClient(base_url="http://comfy.test")
    client._http = httpx.AsyncClient(transport=transport)

    audio = tmp_path / "ref.wav"
    audio.write_bytes(b"wav-placeholder")

    async def run():
        try:
            return await client.upload_audio(audio)
        finally:
            await client.close()

    name = asyncio.run(run())
    assert name == "ref.wav"  # bare filename, no calliope/ prefix
    assert sent["url"].endswith("/upload/image")


def test_prepare_media_inputs_vhs_audio(tmp_path):
    """VHS_LoadAudio with an `audio:` widget gets its file uploaded in place."""
    import asyncio
    from calliope.comfyui.client import ComfyUIClient

    audio = tmp_path / "voice.wav"
    audio.write_bytes(b"wav-placeholder")

    client = ComfyUIClient.__new__(ComfyUIClient)  # skip __init__ HTTP client

    async def fake_upload(path, subfolder=""):
        assert Path(path) == audio
        return "voice.wav"

    client.upload_audio = fake_upload  # type: ignore[method-assign]
    wf = {
        "5": {
            "inputs": {"audio:": str(audio)},
            "class_type": "VHS_LoadAudio",
            "_meta": {"title": "Ref Audio (Input:audio)"},
        }
    }
    out = asyncio.run(client.prepare_media_inputs(wf))
    assert out["5"]["inputs"]["audio:"] == "voice.wav"


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
