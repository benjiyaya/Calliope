from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from calliope.db import migrate_db
from calliope.main import create_app


@pytest.fixture
def client(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        import calliope.config as config_module

        # Mutate singleton in place so all `from calliope.config import settings` refs update
        s = config_module.settings
        prev = {
            "data_dir": s.data_dir,
            "assets_dir": s.assets_dir,
            "dry_run": s.dry_run,
            "queue_poll_interval_sec": s.queue_poll_interval_sec,
        }
        s.data_dir = Path(tmpdir)
        s.assets_dir = Path(tmpdir) / "assets"
        s.dry_run = True
        s.queue_poll_interval_sec = 0.5
        # Never persist temp test paths into the real calliope_config.json
        monkeypatch.setattr(config_module.Settings, "save_config_file", lambda self: None)
        asyncio.run(migrate_db(s.db_path))
        app = create_app()
        try:
            with TestClient(app) as c:
                yield c
        finally:
            for k, v in prev.items():
                setattr(s, k, v)
