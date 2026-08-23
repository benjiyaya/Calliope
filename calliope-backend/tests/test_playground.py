from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from calliope.db import migrate_db
from calliope.main import create_app


SAMPLE_WORKFLOW = {
    "1": {
        "class_type": "CLIPTextEncode",
        "inputs": {"text": "hello"},
        "_meta": {"title": "Prompt (Input:prompt)"},
    },
    "2": {
        "class_type": "SaveImage",
        "inputs": {"images": ["1", 0]},
        "_meta": {"title": "Save (Output:image)"},
    },
}


@pytest.fixture
def client(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        import asyncio
        import calliope.config as config_module

        s = config_module.settings
        prev = {
            "data_dir": s.data_dir,
            "assets_dir": s.assets_dir,
            "dry_run": s.dry_run,
        }
        s.data_dir = Path(tmpdir)
        s.assets_dir = Path(tmpdir) / "assets"
        s.assets_dir.mkdir(parents=True, exist_ok=True)
        s.dry_run = True
        monkeypatch.setattr(config_module.Settings, "save_config_file", lambda self: None)
        asyncio.run(migrate_db(s.db_path))
        app = create_app()
        try:
            with TestClient(app) as c:
                yield c
        finally:
            for k, v in prev.items():
                setattr(s, k, v)


def test_playground_generate_and_attach(client):
    wf = client.post(
        "/api/workflows",
        json={"name": "PG Img", "kind": "image", "workflow_json": SAMPLE_WORKFLOW},
    )
    assert wf.status_code == 200
    workflow_id = wf.json()["id"]

    gen = client.post(
        "/api/playground/generate",
        json={"workflow_id": workflow_id, "input_values": {"1": "a cat"}},
    )
    assert gen.status_code == 200
    job = gen.json()["job"]
    assert job["kind"] == "image"
    assert job["status"] == "pending"

    # Scratch project hidden from list
    listed = client.get("/api/projects").json()
    assert all(p.get("status") != "system" for p in listed)

    proj = client.post("/api/projects", json={"title": "Reel"}).json()
    char = client.post(
        f"/api/projects/{proj['id']}/characters",
        json={"name": "Hero"},
    ).json()

    # Wait briefly for dry-run worker, or poll jobs
    import time

    path = None
    done = None
    for _ in range(40):
        jobs = client.get("/api/playground/jobs").json()
        done = next((j for j in jobs if j["id"] == job["id"] and j["status"] == "done"), None)
        if done and done["output_paths"]:
            path = done["output_paths"][0]
            break
        time.sleep(0.25)
    assert path and done, "playground job did not finish in dry-run"

    attach = client.post(
        "/api/playground/attach",
        json={
            "path": path,
            "project_id": proj["id"],
            "target": "character_sheet",
            "character_id": char["id"],
        },
    )
    assert attach.status_code == 200

    assets = client.get(f"/api/projects/{proj['id']}/assets").json()
    sheet = next(c for c in assets["characters"] if c["id"] == char["id"])["sheet_path"]
    assert sheet == path

    from pathlib import Path as P

    assert P(path).is_file()
    dead = client.delete(f"/api/playground/jobs/{done['id']}")
    assert dead.status_code == 200
    assert dead.json()["ok"] is True
    assert path in dead.json()["deleted_files"]
    assert not P(path).exists()
    assert done["id"] not in [j["id"] for j in client.get("/api/playground/jobs").json()]

    assets2 = client.get(f"/api/projects/{proj['id']}/assets").json()
    sheet2 = next(c for c in assets2["characters"] if c["id"] == char["id"])["sheet_path"]
    assert sheet2 is None


def test_playground_attach_item(client):
    wf = client.post(
        "/api/workflows",
        json={"name": "PG Item", "kind": "image", "workflow_json": SAMPLE_WORKFLOW},
    )
    assert wf.status_code == 200
    gen = client.post(
        "/api/playground/generate",
        json={"workflow_id": wf.json()["id"], "input_values": {"1": "a blaster"}},
    )
    assert gen.status_code == 200
    job = gen.json()["job"]

    proj = client.post("/api/projects", json={"title": "Gear"}).json()
    existing = client.post(
        f"/api/projects/{proj['id']}/items",
        json={"name": "Old Prop", "description": "must stay untouched"},
    ).json()

    import time

    path = None
    for _ in range(40):
        jobs = client.get("/api/playground/jobs").json()
        done = next((j for j in jobs if j["id"] == job["id"] and j["status"] == "done"), None)
        if done and done["output_paths"]:
            path = done["output_paths"][0]
            break
        time.sleep(0.25)
    assert path, "playground job did not finish in dry-run"

    attach = client.post(
        "/api/playground/attach",
        json={
            "path": path,
            "project_id": proj["id"],
            "target": "item",
            "name": "Alien Blaster",
            "item_id": existing["id"],
        },
    )
    assert attach.status_code == 200, attach.text
    created_id = attach.json()["item_id"]
    assert created_id != existing["id"]

    assets = client.get(f"/api/projects/{proj['id']}/assets").json()
    items = assets["items"]
    old = next(i for i in items if i["id"] == existing["id"])
    new = next(i for i in items if i["id"] == created_id)
    assert old["name"] == "Old Prop"
    assert old["reference_image_path"] is None
    assert new["name"] == "Alien Blaster"
    assert new["reference_image_path"] == path
