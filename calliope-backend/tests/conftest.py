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


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    """A temp ``data_dir`` plus a ``run`` that drives the real CLI entry point.

    Every CLI test goes through ``calliope.cli.main.main(argv)`` rather than
    calling handlers directly, so what is tested is what an agent types --
    including argument parsing, the policy check, and the exit code.

    Not autouse: the rest of the suite must not be affected by it.
    """
    import io
    import json
    from contextlib import redirect_stderr, redirect_stdout

    import calliope.config as config_module
    from calliope.db import get_db

    monkeypatch.setattr(config_module.settings, "data_dir", tmp_path)
    monkeypatch.setattr(config_module.settings, "assets_dir", tmp_path / "assets")
    monkeypatch.setattr(config_module.Settings, "save_config_file", lambda self: None)
    asyncio.run(migrate_db(config_module.settings.db_path))

    from calliope.cli.main import main

    def run(*argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = main([str(a) for a in argv])
            except SystemExit as exc:
                # argparse refusing an unknown command exits 2 via sys.exit.
                # That IS the boundary working, so it is reported as a code.
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def run_json(*argv):
        code, out, err = run(*argv, "--json")
        return code, (json.loads(out) if code == 0 and out.strip() else None), err

    def payload(name, data):
        p = tmp_path / name
        p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return p

    conn = get_db(config_module.settings.db_path)
    # Autocommit: the CLI opens its own connection and commits. Without this the
    # first SELECT here would pin a WAL read snapshot, and every later assertion
    # would be reading the state from before the write.
    conn.isolation_level = None

    def logs(project_id):
        """Audit rows for one project, oldest first. The project itself is
        created through the CLI, so it has an entry of its own -- tests about a
        specific command filter by command rather than counting everything."""
        code, out, err = run_json("log", "list", "--project", str(project_id))
        assert code == 0, err
        return list(reversed(out))

    try:
        pid = run_json("project", "create", "--title", "Demo")[1]["project_id"]
        yield {
            "run": run,
            "json": run_json,
            "payload": payload,
            "conn": conn,
            "data_dir": tmp_path,
            "project_id": pid,
            "logs": logs,
        }
    finally:
        conn.close()
