from __future__ import annotations

import importlib.metadata
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from calliope.config import Settings
from calliope.main import create_app
from calliope.db import migrate_db


@pytest.fixture
def client(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        import calliope.config as config_module
        import asyncio

        s = config_module.settings
        prev = {
            "data_dir": s.data_dir,
            "assets_dir": s.assets_dir,
            "dry_run": s.dry_run,
        }
        s.data_dir = Path(tmpdir)
        s.assets_dir = Path(tmpdir) / "assets"
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


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_health_version_matches_package_metadata(client):
    from calliope.main import __version__

    assert client.get("/api/health").json()["version"] == __version__
    # A literal here would recreate the bug this guards against: the string must
    # come from the installed package metadata (pyproject's version), not main.py.
    assert __version__ == importlib.metadata.version("calliope")


def test_create_and_list_project(client):
    r = client.post("/api/projects", json={"title": "Test", "idea": "idea"})
    assert r.status_code == 200
    data = r.json()
    assert data["title"] == "Test"
    assert data["idea"] == "idea"
    pid = data["id"]

    r = client.get("/api/projects")
    assert r.status_code == 200
    assert any(p["id"] == pid for p in r.json())


def test_list_projects_includes_stats(client):
    r = client.post("/api/projects", json={"title": "ListStats"})
    pid = r.json()["id"]
    r = client.get("/api/projects")
    assert r.status_code == 200
    item = next(p for p in r.json() if p["id"] == pid)
    assert item["stats"] == {
        "scene_count": 0,
        "character_count": 0,
        "asset_ready_count": 0,
        "asset_total_count": 0,
    }


def test_get_project_with_stats(client):
    r = client.post("/api/projects", json={"title": "Stats"})
    pid = r.json()["id"]
    r = client.get(f"/api/projects/{pid}")
    assert r.status_code == 200
    data = r.json()
    assert data["stats"]["scene_count"] == 0


def test_update_project(client):
    r = client.post("/api/projects", json={"title": "Old"})
    pid = r.json()["id"]
    r = client.patch(f"/api/projects/{pid}", json={"title": "New"})
    assert r.status_code == 200
    assert r.json()["title"] == "New"


def test_update_project_cover(client):
    r = client.post("/api/projects", json={"title": "Cover"})
    pid = r.json()["id"]
    assert r.json()["cover_path"] is None

    r = client.patch(f"/api/projects/{pid}", json={"cover_path": "/tmp/cover.png"})
    assert r.status_code == 200
    assert r.json()["cover_path"] == "/tmp/cover.png"

    r = client.get(f"/api/projects/{pid}")
    assert r.json()["cover_path"] == "/tmp/cover.png"
    r = client.get("/api/projects")
    assert next(p for p in r.json() if p["id"] == pid)["cover_path"] == "/tmp/cover.png"

    # Explicit null clears the cover again.
    r = client.patch(f"/api/projects/{pid}", json={"cover_path": None})
    assert r.status_code == 200
    assert r.json()["cover_path"] is None

    # Unrelated patches leave the (cleared) cover untouched.
    r = client.patch(f"/api/projects/{pid}", json={"title": "Cover 2"})
    assert r.json()["title"] == "Cover 2"
    assert r.json()["cover_path"] is None


def test_delete_project(client):
    r = client.post("/api/projects", json={"title": "Delete"})
    pid = r.json()["id"]
    r = client.delete(f"/api/projects/{pid}")
    assert r.status_code == 200
    r = client.get(f"/api/projects/{pid}")
    assert r.status_code == 404
