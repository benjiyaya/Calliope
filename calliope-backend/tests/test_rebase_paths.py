"""Tests for rebasing stored asset paths after the install folder moves."""
from __future__ import annotations

import json

from calliope.db import SCHEMA, rebase_stale_asset_paths, get_db


def _make_db(tmp_path):
    db_path = tmp_path / "calliope.db"
    conn = get_db(db_path)
    conn.executescript(SCHEMA)
    return conn


def test_rebase_stale_paths(tmp_path):
    old_root = tmp_path / "old" / "Calliope" / "calliope-backend" / "data"
    new_data = tmp_path / "new" / "data"
    new_assets = new_data / "assets"
    conn = _make_db(tmp_path)
    try:
        conn.execute(
            "INSERT INTO projects (title) VALUES ('demo')",
        )
        conn.execute(
            "INSERT INTO characters (project_id, name, sheet_path) VALUES (1, 'Mia', ?)",
            (str(old_root / "assets" / "1" / "image" / "sheet.png"),),
        )
        conn.execute(
            "INSERT INTO scenes (project_id, order_index, video_path) VALUES (1, 1, ?)",
            (str(old_root / "assets" / "1" / "video" / "clip.mp4"),),
        )
        conn.execute(
            "INSERT INTO jobs (project_id, kind, output_paths_json) VALUES (1, 'image', ?)",
            (json.dumps([str(old_root / "assets" / "1" / "image" / "out.png")]),),
        )
        # A path already under the current install must be left alone
        current = str(new_assets / "2" / "image" / "keep.png")
        conn.execute(
            "INSERT INTO locations (project_id, name, reference_image_path) VALUES (1, 'Lab', ?)",
            (current,),
        )
        conn.commit()

        rebased = rebase_stale_asset_paths(conn, new_data, new_assets)
        conn.commit()

        assert rebased == 3
        row = conn.execute("SELECT sheet_path FROM characters WHERE id = 1").fetchone()
        assert row["sheet_path"] == str(new_assets / "1" / "image" / "sheet.png")
        row = conn.execute("SELECT video_path FROM scenes WHERE id = 1").fetchone()
        assert row["video_path"] == str(new_assets / "1" / "video" / "clip.mp4")
        row = conn.execute("SELECT output_paths_json FROM jobs WHERE id = 1").fetchone()
        assert json.loads(row["output_paths_json"]) == [
            str(new_assets / "1" / "image" / "out.png")
        ]
        row = conn.execute("SELECT reference_image_path FROM locations WHERE id = 1").fetchone()
        assert row["reference_image_path"] == current
    finally:
        conn.close()


def test_rebase_ignores_relative_and_foreign_paths(tmp_path):
    new_data = tmp_path / "data"
    new_assets = new_data / "assets"
    conn = _make_db(tmp_path)
    try:
        conn.execute("INSERT INTO projects (title, cover_path) VALUES ('demo', 'relative/x.png')")
        conn.execute(
            "INSERT INTO characters (project_id, name, sheet_path) VALUES (1, 'A', ?)",
            ("C:/elsewhere/no-assets-segment.png",),
        )
        conn.commit()
        assert rebase_stale_asset_paths(conn, new_data, new_assets) == 0
    finally:
        conn.close()
