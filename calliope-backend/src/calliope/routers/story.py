from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException

from calliope.agent.llm import generate_structured
from calliope.agent.prompts import (
    STORY_GENERATION_SYSTEM,
    build_story_messages,
    character_sheet_prompt,
    item_reference_prompt,
    location_reference_prompt,
    recommend_beat_count,
    story_beats_chunk_prompt,
    story_brief_chunk_prompt,
    story_generation_user_prompt,
)

# Beats per LLM call for long stories. One 50-beat request is a huge JSON
# answer small/local models take minutes to stream (and often under-deliver,
# triggering a full retry) — same failure mode the script path had. Small
# boards stay one call.
STORY_CHUNK = 12
from calliope.config import settings
from calliope.db import get_db, row_to_dict
from calliope.events.bus import event_bus
from calliope.models.schemas import (
    BeatCreate,
    BeatUpdate,
    CharacterCreate,
    CharacterUpdate,
    ItemCreate,
    ItemUpdate,
    LocationCreate,
    LocationUpdate,
)

logger = logging.getLogger("calliope.story")

router = APIRouter()


def _touch_project(conn, project_id: int) -> None:
    conn.execute(
        "UPDATE projects SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (project_id,),
    )


def _require_project(conn, project_id: int) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Project not found")
    return row_to_dict(row)


async def _request_beats_chunk(
    system: str,
    user: str,
    *,
    want: int,
    temperature: float = 0.7,
) -> list[dict[str, Any]]:
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    result = await generate_structured(messages, temperature=temperature)
    beats = result.get("beats") or []
    if len(beats) < want:
        # Retry only this chunk — never the whole story.
        retry = await generate_structured(
            [
                messages[0],
                {
                    "role": "user",
                    "content": user
                    + (
                        f"\n\nPREVIOUS ATTEMPT FAILED: it only had {len(beats)} beats. "
                        f"You MUST return exactly {want} beats this time."
                    ),
                },
            ],
            temperature=0.4,
        )
        beats = retry.get("beats") or []
    return beats


async def _draft_story_chunked(
    project_id: int, project: dict[str, Any], required_beats: int
) -> dict[str, Any]:
    """Beat list in chunks of STORY_CHUNK (brief + first beats in call 1,
    continuations after). Boards that fit one chunk behave exactly like the
    old single call, including the corrective retry."""
    system = STORY_GENERATION_SYSTEM
    if required_beats <= STORY_CHUNK:
        messages = build_story_messages(
            title=project["title"],
            idea=project["idea"],
            genre=project["genre"],
            tone=project["tone"],
            target_duration=project["target_duration"],
        )
        result = await generate_structured(messages, temperature=0.7)
        beats_out = result.get("beats") or []
        if len(beats_out) < required_beats:
            await event_bus.publish(
                "agent.thinking",
                {
                    "project_id": project_id,
                    "message": (
                        f"Model returned {len(beats_out)} beats; "
                        f"required {required_beats}. Retrying..."
                    ),
                },
            )
            repair = [
                {"role": "system", "content": messages[0]["content"]},
                {
                    "role": "user",
                    "content": story_generation_user_prompt(
                        project["title"],
                        project["idea"],
                        project["genre"],
                        project["tone"],
                        project["target_duration"],
                    )
                    + (
                        f"\n\nPREVIOUS ATTEMPT FAILED: it only had {len(beats_out)} beats. "
                        f"You MUST return exactly {required_beats} beats this time."
                    ),
                },
            ]
            result = await generate_structured(repair, temperature=0.4)
            beats_out = result.get("beats") or []
        result = {**result, "beats": beats_out}
        return result

    # 1) Brief + first chunk
    first_n = min(STORY_CHUNK, required_beats)
    brief_user = story_brief_chunk_prompt(
        project["title"],
        project["idea"],
        project["genre"],
        project["tone"],
        project["target_duration"],
        total_beats=required_beats,
        chunk_beats=first_n,
    )
    brief_messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": brief_user},
    ]
    brief = await generate_structured(brief_messages, temperature=0.7)
    beats_out = brief.get("beats") or []
    if len(beats_out) < first_n:
        # _request_beats_chunk retries the short chunk once at low temp.
        beats_out = await _request_beats_chunk(system, brief_user, want=first_n)
    title = brief.get("title") or project["title"]
    logline = brief.get("logline")
    cast_summary = "\n".join(
        [
            *(
                f"- {c.get('name')} ({c.get('role') or ''}): {c.get('appearance') or ''}"
                for c in brief.get("characters") or []
            ),
            *(
                f"- location {l.get('name')}: {l.get('description') or ''}"
                for l in brief.get("locations") or []
            ),
            *(
                f"- item {i.get('name')}: {i.get('description') or ''}"
                for i in brief.get("items") or []
            ),
        ]
    )

    # 2) Continuation chunks
    written = [dict(b) for b in beats_out[:first_n]]
    for offset, b in enumerate(written):
        b["order_index"] = offset + 1
    start = len(written) + 1
    total_calls = (required_beats + STORY_CHUNK - 1) // STORY_CHUNK
    call_no = 2
    while start <= required_beats:
        want = min(STORY_CHUNK, required_beats - start + 1)
        await event_bus.publish(
            "agent.thinking",
            {
                "project_id": project_id,
                "message": f"Writing beats {start}–{start + want - 1} "
                f"(chunk {call_no}/{total_calls})...",
            },
        )
        user = story_beats_chunk_prompt(
            title=title,
            logline=logline,
            genre=project["genre"],
            tone=project["tone"],
            total_beats=required_beats,
            chunk_start=start,
            chunk_beats=want,
            previous_beats=written,
            cast_summary=cast_summary,
        )
        chunk = await _request_beats_chunk(system, user, want=want)
        if not chunk:
            break
        accepted = chunk[:want]
        for offset, b in enumerate(accepted):
            b["order_index"] = start + offset
            written.append(b)
        # Advance by what was actually accepted, so a short chunk never
        # leaves a hole in the 1..N order_index sequence.
        start += len(accepted)
        call_no += 1

    return {**brief, "title": title, "logline": logline, "beats": written}


@router.post("/{project_id}/generate-story")
async def generate_story(project_id: int, replace: bool = True) -> dict[str, Any]:
    conn = get_db(settings.db_path)
    try:
        project = _require_project(conn, project_id)

        required_beats = recommend_beat_count(project.get("target_duration"))
        await event_bus.publish(
            "agent.thinking",
            {
                "project_id": project_id,
                "message": (
                    f"Drafting storyline for \"{project['title']}\" "
                    f"(target {project.get('target_duration') or '?'} -> {required_beats} beats)..."
                ),
            },
        )

        result = await _draft_story_chunked(project_id, project, required_beats)
        beats_out = result.get("beats") or []
        if len(beats_out) < required_beats:
            raise HTTPException(
                status_code=502,
                detail=(
                    f"Story draft returned {len(beats_out)} beats but target "
                    f"'{project.get('target_duration')}' requires {required_beats}. "
                    "Try Draft Storyline again, or check the LLM follows JSON instructions."
                ),
            )

        title = result.get("title") or project["title"]
        logline = result.get("logline")
        conn.execute(
            "UPDATE projects SET title = ?, idea = COALESCE(?, idea), status = 'in_progress', "
            "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (title, logline, project_id),
        )

        if replace:
            conn.execute("DELETE FROM story_beats WHERE project_id = ?", (project_id,))
            conn.execute("DELETE FROM characters WHERE project_id = ?", (project_id,))
            conn.execute("DELETE FROM locations WHERE project_id = ?", (project_id,))
            conn.execute("DELETE FROM items WHERE project_id = ?", (project_id,))
            # scenes.location_id has no FK — the mass delete above would leave
            # every scene pointing at a dead location row.
            conn.execute(
                "UPDATE scenes SET location_id = NULL WHERE project_id = ?", (project_id,)
            )

        for beat in result.get("beats", []):
            conn.execute(
                """
                INSERT INTO story_beats (project_id, order_index, title, description)
                VALUES (:project_id, :order_index, :title, :description)
                """,
                {
                    "project_id": project_id,
                    "order_index": beat.get("order_index", 0),
                    "title": beat.get("title", ""),
                    "description": beat.get("description", ""),
                },
            )

        for character in result.get("characters", []):
            appearance = character.get("appearance", "") or ""
            row_seed = {
                "name": character.get("name", ""),
                "role": character.get("role", ""),
                "age": character.get("age", ""),
                "appearance": appearance,
                "personality": character.get("personality", ""),
            }
            # Seed with the published sheet template so Assets shows a real prompt, not a hidden blob.
            sheet_prompt = character_sheet_prompt(row_seed)
            conn.execute(
                """
                INSERT INTO characters
                (project_id, name, role, age, appearance, personality, consistency_prompt)
                VALUES (:project_id, :name, :role, :age, :appearance, :personality, :consistency_prompt)
                """,
                {
                    "project_id": project_id,
                    **row_seed,
                    "consistency_prompt": sheet_prompt,
                },
            )

        for location in result.get("locations", []):
            description = location.get("description", "") or ""
            loc_seed = {
                "name": location.get("name", ""),
                "description": description,
            }
            env_prompt = location_reference_prompt(loc_seed)
            conn.execute(
                """
                INSERT INTO locations (project_id, name, description, consistency_prompt)
                VALUES (:project_id, :name, :description, :consistency_prompt)
                """,
                {
                    "project_id": project_id,
                    **loc_seed,
                    "consistency_prompt": env_prompt,
                },
            )

        for item in result.get("items", []):
            description = item.get("description", "") or ""
            item_seed = {
                "name": item.get("name", ""),
                "description": description,
            }
            item_prompt = item_reference_prompt(item_seed)
            conn.execute(
                """
                INSERT INTO items (project_id, name, description, consistency_prompt)
                VALUES (:project_id, :name, :description, :consistency_prompt)
                """,
                {
                    "project_id": project_id,
                    **item_seed,
                    "consistency_prompt": item_prompt,
                },
            )

        conn.commit()
        beat_n = len(result.get("beats", []))
        await event_bus.publish(
            "story.ready",
            {
                "project_id": project_id,
                "message": f"Story drafted — {beat_n} beats",
            },
        )
        return {"ok": True, "project_id": project_id, "generated": result}
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Story generation failed for project %s", project_id)
        await event_bus.publish(
            "agent.thinking",
            {"project_id": project_id, "message": f"Story draft failed: {exc}"},
        )
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        conn.close()


@router.get("/{project_id}/story")
async def get_story(project_id: int) -> dict[str, Any]:
    conn = get_db(settings.db_path)
    try:
        project = conn.execute(
            "SELECT id, title, idea, genre, tone, target_duration, status FROM projects WHERE id = ?",
            (project_id,),
        ).fetchone()
        if not project:
            raise HTTPException(status_code=404, detail="Project not found")

        beats = conn.execute(
            "SELECT id, order_index, title, description FROM story_beats WHERE project_id = ? ORDER BY order_index",
            (project_id,),
        ).fetchall()
        characters = conn.execute(
            """
            SELECT id, name, role, age, appearance, personality, portrait_path, sheet_path, consistency_prompt
            FROM characters WHERE project_id = ?
            """,
            (project_id,),
        ).fetchall()
        locations = conn.execute(
            """
            SELECT id, name, description, reference_image_path, consistency_prompt
            FROM locations WHERE project_id = ?
            """,
            (project_id,),
        ).fetchall()
        items = conn.execute(
            """
            SELECT id, name, description, reference_image_path, consistency_prompt
            FROM items WHERE project_id = ?
            """,
            (project_id,),
        ).fetchall()

        return {
            "project": row_to_dict(project),
            "beats": [row_to_dict(b) for b in beats],
            "characters": [row_to_dict(c) for c in characters],
            "locations": [row_to_dict(l) for l in locations],
            "items": [row_to_dict(i) for i in items],
        }
    finally:
        conn.close()


# --- Beats CRUD ---


@router.post("/{project_id}/beats")
async def create_beat(project_id: int, payload: BeatCreate) -> dict[str, Any]:
    conn = get_db(settings.db_path)
    try:
        _require_project(conn, project_id)
        cur = conn.execute(
            """
            INSERT INTO story_beats (project_id, order_index, title, description)
            VALUES (?, ?, ?, ?)
            """,
            (project_id, payload.order_index, payload.title, payload.description),
        )
        _touch_project(conn, project_id)
        conn.commit()
        row = conn.execute("SELECT * FROM story_beats WHERE id = ?", (cur.lastrowid,)).fetchone()
        return row_to_dict(row)
    finally:
        conn.close()


@router.patch("/{project_id}/beats/{beat_id}")
async def update_beat(project_id: int, beat_id: int, payload: BeatUpdate) -> dict[str, Any]:
    conn = get_db(settings.db_path)
    try:
        existing = conn.execute(
            "SELECT * FROM story_beats WHERE id = ? AND project_id = ?",
            (beat_id, project_id),
        ).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail="Beat not found")
        data = {k: v for k, v in payload.model_dump().items() if v is not None}
        if data:
            fields = ", ".join(f"{k} = :{k}" for k in data)
            data["id"] = beat_id
            conn.execute(f"UPDATE story_beats SET {fields} WHERE id = :id", data)
            _touch_project(conn, project_id)
            conn.commit()
        row = conn.execute("SELECT * FROM story_beats WHERE id = ?", (beat_id,)).fetchone()
        return row_to_dict(row)
    finally:
        conn.close()


@router.delete("/{project_id}/beats/{beat_id}")
async def delete_beat(project_id: int, beat_id: int) -> dict[str, bool]:
    conn = get_db(settings.db_path)
    try:
        cur = conn.execute(
            "DELETE FROM story_beats WHERE id = ? AND project_id = ?",
            (beat_id, project_id),
        )
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Beat not found")
        _touch_project(conn, project_id)
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


# --- Characters CRUD ---


@router.post("/{project_id}/characters")
async def create_character(project_id: int, payload: CharacterCreate) -> dict[str, Any]:
    conn = get_db(settings.db_path)
    try:
        _require_project(conn, project_id)
        consistency = payload.consistency_prompt or character_sheet_prompt(
            {
                "name": payload.name,
                "role": payload.role,
                "age": payload.age,
                "appearance": payload.appearance,
                "personality": payload.personality,
            }
        )
        cur = conn.execute(
            """
            INSERT INTO characters
            (project_id, name, role, age, appearance, personality, consistency_prompt)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                project_id,
                payload.name,
                payload.role,
                payload.age,
                payload.appearance,
                payload.personality,
                consistency,
            ),
        )
        _touch_project(conn, project_id)
        conn.commit()
        row = conn.execute("SELECT * FROM characters WHERE id = ?", (cur.lastrowid,)).fetchone()
        return row_to_dict(row)
    finally:
        conn.close()


@router.patch("/{project_id}/characters/{character_id}")
async def update_character(
    project_id: int, character_id: int, payload: CharacterUpdate
) -> dict[str, Any]:
    conn = get_db(settings.db_path)
    try:
        existing = conn.execute(
            "SELECT * FROM characters WHERE id = ? AND project_id = ?",
            (character_id, project_id),
        ).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail="Character not found")
        data = {k: v for k, v in payload.model_dump().items() if v is not None}
        if data:
            fields = ", ".join(f"{k} = :{k}" for k in data)
            data["id"] = character_id
            conn.execute(f"UPDATE characters SET {fields} WHERE id = :id", data)
            _touch_project(conn, project_id)
            conn.commit()
        row = conn.execute("SELECT * FROM characters WHERE id = ?", (character_id,)).fetchone()
        return row_to_dict(row)
    finally:
        conn.close()


@router.delete("/{project_id}/characters/{character_id}")
async def delete_character(project_id: int, character_id: int) -> dict[str, bool]:
    conn = get_db(settings.db_path)
    try:
        cur = conn.execute(
            "DELETE FROM characters WHERE id = ? AND project_id = ?",
            (character_id, project_id),
        )
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Character not found")
        _touch_project(conn, project_id)
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


# --- Locations CRUD ---


@router.post("/{project_id}/locations")
async def create_location(project_id: int, payload: LocationCreate) -> dict[str, Any]:
    conn = get_db(settings.db_path)
    try:
        _require_project(conn, project_id)
        consistency = payload.consistency_prompt or location_reference_prompt(
            {"name": payload.name, "description": payload.description}
        )
        cur = conn.execute(
            """
            INSERT INTO locations (project_id, name, description, consistency_prompt)
            VALUES (?, ?, ?, ?)
            """,
            (project_id, payload.name, payload.description, consistency),
        )
        _touch_project(conn, project_id)
        conn.commit()
        row = conn.execute("SELECT * FROM locations WHERE id = ?", (cur.lastrowid,)).fetchone()
        return row_to_dict(row)
    finally:
        conn.close()


@router.patch("/{project_id}/locations/{location_id}")
async def update_location(
    project_id: int, location_id: int, payload: LocationUpdate
) -> dict[str, Any]:
    conn = get_db(settings.db_path)
    try:
        existing = conn.execute(
            "SELECT * FROM locations WHERE id = ? AND project_id = ?",
            (location_id, project_id),
        ).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail="Location not found")
        data = {k: v for k, v in payload.model_dump().items() if v is not None}
        if data:
            fields = ", ".join(f"{k} = :{k}" for k in data)
            data["id"] = location_id
            conn.execute(f"UPDATE locations SET {fields} WHERE id = :id", data)
            _touch_project(conn, project_id)
            conn.commit()
        row = conn.execute("SELECT * FROM locations WHERE id = ?", (location_id,)).fetchone()
        return row_to_dict(row)
    finally:
        conn.close()


@router.delete("/{project_id}/locations/{location_id}")
async def delete_location(project_id: int, location_id: int) -> dict[str, bool]:
    conn = get_db(settings.db_path)
    try:
        cur = conn.execute(
            "DELETE FROM locations WHERE id = ? AND project_id = ?",
            (location_id, project_id),
        )
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Location not found")
        _touch_project(conn, project_id)
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


# --- Items CRUD ---


@router.post("/{project_id}/items")
async def create_item(project_id: int, payload: ItemCreate) -> dict[str, Any]:
    conn = get_db(settings.db_path)
    try:
        _require_project(conn, project_id)
        consistency = payload.consistency_prompt or item_reference_prompt(
            {"name": payload.name, "description": payload.description}
        )
        cur = conn.execute(
            """
            INSERT INTO items (project_id, name, description, consistency_prompt)
            VALUES (?, ?, ?, ?)
            """,
            (project_id, payload.name, payload.description, consistency),
        )
        _touch_project(conn, project_id)
        conn.commit()
        row = conn.execute("SELECT * FROM items WHERE id = ?", (cur.lastrowid,)).fetchone()
        return row_to_dict(row)
    finally:
        conn.close()


@router.patch("/{project_id}/items/{item_id}")
async def update_item(
    project_id: int, item_id: int, payload: ItemUpdate
) -> dict[str, Any]:
    conn = get_db(settings.db_path)
    try:
        existing = conn.execute(
            "SELECT * FROM items WHERE id = ? AND project_id = ?",
            (item_id, project_id),
        ).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail="Item not found")
        data = {k: v for k, v in payload.model_dump().items() if v is not None}
        if data:
            fields = ", ".join(f"{k} = :{k}" for k in data)
            data["id"] = item_id
            conn.execute(f"UPDATE items SET {fields} WHERE id = :id", data)
            _touch_project(conn, project_id)
            conn.commit()
        row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        return row_to_dict(row)
    finally:
        conn.close()


@router.delete("/{project_id}/items/{item_id}")
async def delete_item(project_id: int, item_id: int) -> dict[str, bool]:
    conn = get_db(settings.db_path)
    try:
        cur = conn.execute(
            "DELETE FROM items WHERE id = ? AND project_id = ?",
            (item_id, project_id),
        )
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Item not found")
        _touch_project(conn, project_id)
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()
