from __future__ import annotations

from fastapi import APIRouter, HTTPException

from calliope.config import settings
from calliope.db import get_db, row_to_dict
from calliope.models.schemas import Project, ProjectCreate, ProjectUpdate

router = APIRouter()


def _project_stats(project_id: int, conn) -> dict[str, int]:
    cur = conn.execute(
        """
        SELECT
            (SELECT COUNT(*) FROM scenes WHERE project_id = :pid) AS scene_count,
            (SELECT COUNT(*) FROM characters WHERE project_id = :pid) AS character_count,
            (SELECT COUNT(*) FROM characters WHERE project_id = :pid AND sheet_path IS NOT NULL)
                + (SELECT COUNT(*) FROM locations WHERE project_id = :pid AND reference_image_path IS NOT NULL) AS asset_ready_count,
            (SELECT COUNT(*) FROM characters WHERE project_id = :pid)
                + (SELECT COUNT(*) FROM locations WHERE project_id = :pid) AS asset_total_count
        """,
        {"pid": project_id},
    )
    return dict(cur.fetchone())


@router.post("", response_model=Project)
async def create_project(payload: ProjectCreate):
    conn = get_db(settings.db_path)
    try:
        cur = conn.execute(
            """
            INSERT INTO projects (title, idea, genre, tone, target_duration)
            VALUES (:title, :idea, :genre, :tone, :target_duration)
            """,
            payload.model_dump(),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM projects WHERE id = ?", (cur.lastrowid,)).fetchone()
        return row_to_dict(row)
    finally:
        conn.close()


@router.get("", response_model=list[Project])
async def list_projects():
    conn = get_db(settings.db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM projects WHERE status != 'system' ORDER BY updated_at DESC"
        ).fetchall()
        out = []
        for row in rows:
            project = row_to_dict(row)
            project["stats"] = _project_stats(row["id"], conn)
            out.append(project)
        return out
    finally:
        conn.close()


@router.get("/{project_id}")
async def get_project(project_id: int):
    conn = get_db(settings.db_path)
    try:
        row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Project not found")
        project = row_to_dict(row)
        project["stats"] = _project_stats(project_id, conn)
        return project
    finally:
        conn.close()


@router.patch("/{project_id}", response_model=Project)
async def update_project(project_id: int, payload: ProjectUpdate):
    conn = get_db(settings.db_path)
    try:
        existing = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail="Project not found")

        # exclude_unset keeps explicitly-sent nulls (e.g. clearing cover_path)
        # while leaving fields the client did not send untouched.
        data = payload.model_dump(exclude_unset=True)
        if not data:
            return row_to_dict(existing)

        fields = ", ".join(f"{k} = :{k}" for k in data.keys())
        data["id"] = project_id
        conn.execute(
            f"UPDATE projects SET {fields}, updated_at = CURRENT_TIMESTAMP WHERE id = :id",
            data,
        )
        conn.commit()
        row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        return row_to_dict(row)
    finally:
        conn.close()


@router.delete("/{project_id}")
async def delete_project(project_id: int):
    from calliope.agent.harness.runner import runner

    conn = get_db(settings.db_path)
    try:
        row = conn.execute("SELECT id FROM projects WHERE id = ?", (project_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Project not found")
        # An agent mid-run would keep firing tools against a dead project id;
        # the FK then silently unlinks its session. Refuse while any linked
        # session is running (same contract as session deletion). Must run
        # BEFORE the DELETE: ON DELETE SET NULL would otherwise clear the
        # links mid-transaction and hide the running sessions.
        running = [
            sid
            for (sid,) in conn.execute(
                "SELECT id FROM agent_sessions WHERE project_id = ?", (project_id,)
            ).fetchall()
            if runner.is_running(sid)
        ]
        if running:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Project has running agent session(s) "
                    f"({', '.join(map(str, running))}). Cancel or wait for them first."
                ),
            )
        conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()
