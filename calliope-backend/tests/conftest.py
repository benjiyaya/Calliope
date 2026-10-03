from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

# MUST run before `import calliope.*`: config.py resolves CONFIG_FILE at import
# time from this variable. The per-test `client` fixture already no-ops
# save_config_file(), but tests that exercise the save path directly (and the
# settings router's endpoints) do write it for real, so the whole session is
# redirected to a scratch file. Without this the suite overwrites the
# operator's live calliope_config.json — which silently reverted a probed
# context window to 0 and wiped tuned settings (observed 2026-10-03).
os.environ.setdefault(
    "CALLIOPE_CONFIG_FILE",
    str(Path(tempfile.gettempdir()) / "calliope_test_config.json"),
)

import pytest
from fastapi.testclient import TestClient

import calliope.config as config_module
from calliope.db import migrate_db
from calliope.main import create_app


@pytest.fixture(scope="session", autouse=True)
def _tests_never_touch_the_real_config():
    """Belt-and-braces: even if CONFIG_FILE was resolved before the override.

    conftest sets the env var first, so this should already hold — but the
    failure mode is silent data loss in the operator's real config, which is
    worth a second, explicit assertion at the source.
    """
    real = config_module.BACKEND_ROOT / "calliope_config.json"
    assert config_module.CONFIG_FILE.resolve() != real.resolve(), (
        "tests would write the live calliope_config.json"
    )


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
