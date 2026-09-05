from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException

from calliope.comfyui.parser import parse_dynamic_inputs, parse_dynamic_outputs
from calliope.comfyui.profiles import detect_prompt_profile
from calliope.config import settings
from calliope.db import get_db, row_to_dict
from calliope.models.schemas import WorkflowAnalyze, WorkflowCreate, WorkflowUpdate

router = APIRouter()


def _serialize_workflow(row: Any) -> dict[str, Any]:
    data = row_to_dict(row)
    data["workflow_json"] = json.loads(data["workflow_json"])
    data["input_schema"] = json.loads(data["input_schema"]) if data.get("input_schema") else []
    data["output_schema"] = json.loads(data["output_schema"]) if data.get("output_schema") else []
    data["is_enabled"] = bool(data.get("is_enabled"))
    return data


@router.post("/analyze")
async def analyze_workflow(payload: WorkflowAnalyze) -> dict[str, Any]:
    inputs = parse_dynamic_inputs(payload.workflow_json)
    outputs = parse_dynamic_outputs(payload.workflow_json)
    return {
        "inputs": inputs,
        "outputs": outputs,
        "suggested_profile": detect_prompt_profile(payload.workflow_json),
    }


@router.post("/{workflow_id}/reanalyze")
async def reanalyze_workflow(workflow_id: int) -> dict[str, Any]:
    """Recompute input/output schemas from the stored workflow JSON.

    Workflows imported before a parser upgrade keep stale (often empty)
    schemas — this refreshes them without re-importing.
    """
    conn = get_db(settings.db_path)
    try:
        row = conn.execute("SELECT * FROM workflows WHERE id = ?", (workflow_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Workflow not found")
        data = row_to_dict(row)
        workflow = json.loads(data["workflow_json"])
        inputs = parse_dynamic_inputs(workflow)
        outputs = parse_dynamic_outputs(workflow)
        conn.execute(
            "UPDATE workflows SET input_schema = ?, output_schema = ? WHERE id = ?",
            (json.dumps(inputs), json.dumps(outputs), workflow_id),
        )
        conn.commit()
        fresh = conn.execute(
            "SELECT * FROM workflows WHERE id = ?", (workflow_id,)
        ).fetchone()
        return _serialize_workflow(fresh)
    finally:
        conn.close()


@router.get("")
async def list_workflows() -> list[dict[str, Any]]:
    conn = get_db(settings.db_path)
    try:
        rows = conn.execute("SELECT * FROM workflows ORDER BY id DESC").fetchall()
        return [_serialize_workflow(r) for r in rows]
    finally:
        conn.close()


@router.post("/dedupe")
async def dedupe_workflows() -> dict[str, Any]:
    """Delete byte-identical duplicate workflows, keeping the lowest id.

    Two rows are duplicates only when name, kind AND workflow_json all match —
    same name with different JSON is a user's variant, never touched.
    """
    conn = get_db(settings.db_path)
    try:
        rows = conn.execute(
            "SELECT id, name, kind, workflow_json FROM workflows ORDER BY id"
        ).fetchall()
        seen: dict[tuple[str, str, str], int] = {}
        drop_ids: list[int] = []
        for r in rows:
            key = (r["name"], r["kind"], r["workflow_json"])
            if key in seen:
                drop_ids.append(r["id"])
            else:
                seen[key] = r["id"]
        removed = 0
        for wid in drop_ids:
            cur = conn.execute("DELETE FROM workflows WHERE id = ?", (wid,))
            removed += cur.rowcount
        conn.commit()
        return {
            "ok": True,
            "removed": removed,
            "kept_ids": sorted(seen.values()),
            "removed_ids": drop_ids,
        }
    finally:
        conn.close()


@router.post("")
async def create_workflow(payload: WorkflowCreate) -> dict[str, Any]:
    inputs = parse_dynamic_inputs(payload.workflow_json)
    outputs = parse_dynamic_outputs(payload.workflow_json)
    profile = payload.prompt_profile or detect_prompt_profile(payload.workflow_json)
    conn = get_db(settings.db_path)
    try:
        # Byte-identical re-imports are the main way the library fills up with
        # same-named clones (the "@" typeahead then shows 20 identical rows).
        existing = conn.execute(
            "SELECT id, name FROM workflows WHERE name = ? AND kind = ? AND workflow_json = ?",
            (payload.name, payload.kind, json.dumps(payload.workflow_json)),
        ).fetchone()
        if existing:
            raise HTTPException(
                status_code=409,
                detail=f"An identical workflow already exists (id {existing['id']}, “{existing['name']}”).",
            )
        cur = conn.execute(
            """
            INSERT INTO workflows (name, kind, workflow_json, input_schema,
                                   output_schema, description, prompt_profile, is_enabled)
            VALUES (?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (
                payload.name,
                payload.kind,
                json.dumps(payload.workflow_json),
                json.dumps(inputs),
                json.dumps(outputs),
                payload.description,
                profile,
            ),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM workflows WHERE id = ?", (cur.lastrowid,)).fetchone()
        return _serialize_workflow(row)
    finally:
        conn.close()


@router.get("/{workflow_id}")
async def get_workflow(workflow_id: int) -> dict[str, Any]:
    conn = get_db(settings.db_path)
    try:
        row = conn.execute("SELECT * FROM workflows WHERE id = ?", (workflow_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Workflow not found")
        return _serialize_workflow(row)
    finally:
        conn.close()


@router.patch("/{workflow_id}")
async def update_workflow(workflow_id: int, payload: WorkflowUpdate) -> dict[str, Any]:
    conn = get_db(settings.db_path)
    try:
        existing = conn.execute("SELECT * FROM workflows WHERE id = ?", (workflow_id,)).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail="Workflow not found")
        data = payload.model_dump(exclude_unset=True)
        if "is_enabled" in data and data["is_enabled"] is not None:
            data["is_enabled"] = 1 if data["is_enabled"] else 0
        data = {k: v for k, v in data.items() if v is not None}
        if data:
            fields = ", ".join(f"{k} = :{k}" for k in data)
            data["id"] = workflow_id
            conn.execute(f"UPDATE workflows SET {fields} WHERE id = :id", data)
            conn.commit()
        row = conn.execute("SELECT * FROM workflows WHERE id = ?", (workflow_id,)).fetchone()
        return _serialize_workflow(row)
    finally:
        conn.close()


@router.delete("/{workflow_id}")
async def delete_workflow(workflow_id: int) -> dict[str, bool]:
    conn = get_db(settings.db_path)
    try:
        cur = conn.execute("DELETE FROM workflows WHERE id = ?", (workflow_id,))
        conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Workflow not found")
        return {"ok": True}
    finally:
        conn.close()
