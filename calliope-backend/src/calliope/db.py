from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    idea TEXT,
    genre TEXT,
    tone TEXT,
    target_duration TEXT,
    cover_path TEXT,
    status TEXT NOT NULL DEFAULT 'draft',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS story_beats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    order_index INTEGER NOT NULL,
    title TEXT NOT NULL,
    description TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS characters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    role TEXT,
    age TEXT,
    appearance TEXT,
    personality TEXT,
    portrait_path TEXT,
    sheet_path TEXT,
    consistency_prompt TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS locations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    description TEXT,
    reference_image_path TEXT,
    consistency_prompt TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    description TEXT,
    reference_image_path TEXT,
    consistency_prompt TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS scenes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    beat_id INTEGER REFERENCES story_beats(id) ON DELETE SET NULL,
    order_index INTEGER NOT NULL,
    heading TEXT,
    action TEXT,
    dialog TEXT,
    duration_sec INTEGER,
    workflow_id INTEGER,
    env_image_path TEXT,
    location_id INTEGER,
    video_path TEXT,
    video_settings_json TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS scene_characters (
    scene_id INTEGER NOT NULL REFERENCES scenes(id) ON DELETE CASCADE,
    character_id INTEGER NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
    PRIMARY KEY (scene_id, character_id)
);

CREATE TABLE IF NOT EXISTS workflows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('image', 'video')),
    workflow_json TEXT NOT NULL,
    input_node_map TEXT,
    output_node_name TEXT,
    input_schema TEXT,
    output_schema TEXT,
    description TEXT,
    prompt_profile TEXT NOT NULL DEFAULT 'prose',
    is_enabled INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    scene_id INTEGER REFERENCES scenes(id) ON DELETE SET NULL,
    kind TEXT NOT NULL,
    workflow_id INTEGER REFERENCES workflows(id) ON DELETE SET NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    payload_json TEXT,
    output_paths_json TEXT,
    error TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    started_at TIMESTAMP,
    completed_at TIMESTAMP,
    retry_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS agent_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL,
    title TEXT NOT NULL DEFAULT 'New chat',
    status TEXT NOT NULL DEFAULT 'idle' CHECK(status IN ('idle', 'running', 'error')),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_agent_sessions_project ON agent_sessions(project_id);

CREATE TABLE IF NOT EXISTS agent_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES agent_sessions(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK(role IN ('user', 'assistant', 'tool', 'system')),
    content TEXT NOT NULL DEFAULT '',
    agent_name TEXT,
    tool_name TEXT,
    tool_args_json TEXT,
    tool_result_json TEXT,
    status TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_agent_messages_session ON agent_messages(session_id);

CREATE TABLE IF NOT EXISTS agent_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES agent_sessions(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    type TEXT NOT NULL,
    data_json TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(session_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_agent_events_session ON agent_events(session_id);
"""


def get_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    # WAL: readers never block writers (the UI polls jobs/settings constantly),
    # and busy_timeout makes writers wait instead of erroring on contention.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _sqlite_connect(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path)


async def migrate_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    # Additive migrations for existing DBs
    cols = {r[1] for r in conn.execute("PRAGMA table_info(workflows)").fetchall()}
    if "input_schema" not in cols:
        conn.execute("ALTER TABLE workflows ADD COLUMN input_schema TEXT")
    if "output_schema" not in cols:
        conn.execute("ALTER TABLE workflows ADD COLUMN output_schema TEXT")
    if "description" not in cols:
        conn.execute("ALTER TABLE workflows ADD COLUMN description TEXT")
    if "prompt_profile" not in cols:
        conn.execute(
            "ALTER TABLE workflows ADD COLUMN prompt_profile TEXT NOT NULL DEFAULT 'prose'"
        )
    scene_cols = {r[1] for r in conn.execute("PRAGMA table_info(scenes)").fetchall()}
    if "location_id" not in scene_cols:
        conn.execute("ALTER TABLE scenes ADD COLUMN location_id INTEGER")
    if "video_path" not in scene_cols:
        conn.execute("ALTER TABLE scenes ADD COLUMN video_path TEXT")
    if "chain_from_prev" not in scene_cols:
        conn.execute(
            "ALTER TABLE scenes ADD COLUMN chain_from_prev INTEGER NOT NULL DEFAULT 0"
        )
    if "video_settings_json" not in scene_cols:
        conn.execute("ALTER TABLE scenes ADD COLUMN video_settings_json TEXT")
    project_cols = {r[1] for r in conn.execute("PRAGMA table_info(projects)").fetchall()}
    if "cover_path" not in project_cols:
        conn.execute("ALTER TABLE projects ADD COLUMN cover_path TEXT")
    conn.commit()
    conn.close()


def row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


# Columns that store absolute asset paths. When the app folder moves, these
# still point at the old install root and /api/file rejects them (403).
_PATH_COLUMNS = {
    "projects": ["cover_path"],
    "characters": ["portrait_path", "sheet_path"],
    "locations": ["reference_image_path"],
    "items": ["reference_image_path"],
    "scenes": ["env_image_path", "video_path"],
}


def _rebase_path(value: Any, data_dir: Path, assets_dir: Path) -> str | None:
    """Rebase an absolute path from a previous install location onto current dirs.

    Returns the rebased path, or None when the value is not a stale absolute
    path (relative, already under data_dir, or has no 'assets' segment).
    """
    if not value or not isinstance(value, str):
        return None
    p = Path(value)
    if not p.is_absolute():
        return None
    try:
        p.resolve().relative_to(data_dir.resolve())
        return None  # already under the current install
    except (ValueError, OSError):
        pass
    parts_lower = [seg.lower() for seg in p.parts]
    if "assets" not in parts_lower:
        return None
    idx = len(parts_lower) - 1 - parts_lower[::-1].index("assets")
    rel = Path(*p.parts[idx + 1 :])
    if not rel.parts:
        return None
    return str(assets_dir / rel)


def rebase_stale_asset_paths(conn: sqlite3.Connection, data_dir: Path, assets_dir: Path) -> int:
    """Rewrite stored asset paths left over from a previous install location.

    Call after migrate_db (needs all columns to exist). Returns the number of
    rewritten values; caller commits.
    """
    count = 0
    for table, columns in _PATH_COLUMNS.items():
        for col in columns:
            rows = conn.execute(f"SELECT id, {col} FROM {table} WHERE {col} IS NOT NULL").fetchall()
            for row in rows:
                new = _rebase_path(row[col], data_dir, assets_dir)
                if new and new != row[col]:
                    conn.execute(f"UPDATE {table} SET {col} = ? WHERE id = ?", (new, row["id"]))
                    count += 1
    rows = conn.execute(
        "SELECT id, output_paths_json FROM jobs WHERE output_paths_json IS NOT NULL"
    ).fetchall()
    for row in rows:
        try:
            paths = json.loads(row["output_paths_json"] or "[]")
        except json.JSONDecodeError:
            continue
        if not isinstance(paths, list):
            continue
        new_paths = [_rebase_path(p, data_dir, assets_dir) or p for p in paths]
        if new_paths != paths:
            conn.execute(
                "UPDATE jobs SET output_paths_json = ? WHERE id = ?",
                (json.dumps(new_paths), row["id"]),
            )
            count += 1
    # Scene video settings embed absolute asset paths (ref images, uploaded
    # clips) in input_values — rebase those too so a moved install keeps the
    # saved setups working.
    rows = conn.execute(
        "SELECT id, video_settings_json FROM scenes WHERE video_settings_json IS NOT NULL"
    ).fetchall()
    for row in rows:
        try:
            data = json.loads(row["video_settings_json"] or "{}")
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        values = data.get("input_values")
        if isinstance(values, dict):
            data["input_values"] = {
                k: (_rebase_path(v, data_dir, assets_dir) or v if isinstance(v, str) else v)
                for k, v in values.items()
            }
        draft = data.get("prompt_draft")
        if isinstance(draft, str) and not draft:
            data.pop("prompt_draft", None)
        conn.execute(
            "UPDATE scenes SET video_settings_json = ? WHERE id = ?",
            (json.dumps(data), row["id"]),
        )
        count += 1
    return count
