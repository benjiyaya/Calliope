from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from calliope.db import migrate_db
from calliope.main import create_app


@pytest.fixture
def spa_client(monkeypatch, tmp_path: Path):
    import calliope.config as config_module

    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<!doctype html><title>SPA</title>ok", encoding="utf-8")
    (static / "hello.txt").write_text("hi", encoding="utf-8")

    s = config_module.settings
    prev = {
        "data_dir": s.data_dir,
        "assets_dir": s.assets_dir,
        "dry_run": s.dry_run,
    }
    s.data_dir = tmp_path / "data"
    s.assets_dir = s.data_dir / "assets"
    s.dry_run = True
    monkeypatch.setattr(config_module.Settings, "save_config_file", lambda self: None)
    asyncio.run(migrate_db(s.db_path))
    app = create_app(static_dir=static)
    try:
        with TestClient(app) as c:
            yield c
    finally:
        for k, v in prev.items():
            setattr(s, k, v)


def test_client_route_serves_index_html(spa_client):
    r = spa_client.get("/project/12")
    assert r.status_code == 200
    assert "SPA" in r.text
    assert r.headers.get("cache-control") == "no-store"


def test_projects_path_serves_index_html(spa_client):
    r = spa_client.get("/projects")
    assert r.status_code == 200
    assert "SPA" in r.text


def test_real_static_file_still_served(spa_client):
    r = spa_client.get("/hello.txt")
    assert r.status_code == 200
    assert r.text == "hi"


def test_missing_js_stays_404(spa_client):
    r = spa_client.get("/_app/missing.js")
    assert r.status_code == 404


def test_api_still_json_404(spa_client):
    r = spa_client.get("/api/does-not-exist")
    assert r.status_code == 404
    assert r.json()["detail"].startswith("API route not found")


def test_health_untouched(spa_client):
    r = spa_client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
