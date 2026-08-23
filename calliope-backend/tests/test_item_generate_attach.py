"""Misc. Item generate jobs must attach the downloaded file to the item row."""
from __future__ import annotations

from calliope.config import settings
from calliope.db import get_db
from calliope.queue.worker import queue_worker


def test_item_generate_attaches_reference_image(client):
    pid = client.post("/api/projects", json={"title": "Item Attach"}).json()["id"]
    item = client.post(
        f"/api/projects/{pid}/items",
        json={"name": "Mecha Suits", "description": "exoskeletons"},
    ).json()
    assert item.get("reference_image_path") in (None, "")

    dest = settings.assets_dir / str(pid) / "image"
    dest.mkdir(parents=True, exist_ok=True)
    path = dest / "mecha.png"
    path.write_bytes(b"\x89PNG\r\n")

    queue_worker._apply_outputs_to_entities(
        {"kind": "image", "scene_id": None},
        {"item_id": item["id"]},
        [str(path)],
    )

    assets = client.get(f"/api/projects/{pid}/assets").json()
    row = next(i for i in assets["items"] if i["id"] == item["id"])
    assert row["reference_image_path"] == str(path)

    conn = get_db(settings.db_path)
    try:
        db_row = conn.execute(
            "SELECT reference_image_path FROM items WHERE id = ?", (item["id"],)
        ).fetchone()
    finally:
        conn.close()
    assert db_row["reference_image_path"] == str(path)


def test_item_job_label_uses_name(client):
    pid = client.post("/api/projects", json={"title": "Item Label"}).json()["id"]
    item = client.post(
        f"/api/projects/{pid}/items",
        json={"name": "Energy Shield", "description": "bubble"},
    ).json()
    label = queue_worker._job_label(
        {"kind": "image", "id": 301, "payload_json": f'{{"item_id": {item["id"]}}}'},
    )
    assert label == "Energy Shield · item"
