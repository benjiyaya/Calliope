"""Shared SQLite helpers for studio tools."""

from __future__ import annotations

from typing import Any

from calliope.config import settings
from calliope.db import get_db


def connect():
    return get_db(settings.db_path)


def as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def touch(conn, project_id: int) -> None:
    conn.execute(
        "UPDATE projects SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (project_id,),
    )
