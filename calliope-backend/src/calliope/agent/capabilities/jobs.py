"""Wait for image and video jobs the user already approved."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from calliope.agent.capabilities.sql import connect
from calliope.config import settings
from calliope.db import row_to_dict

_TERMINAL = {"completed", "failed", "cancelled"}


def _outputs(row: dict[str, Any]) -> list[str]:
    raw = row.get("output_paths_json")
    if not raw:
        return []
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        return []
    found: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, str) and value not in found:
            found.append(value)
        elif isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(data)
    return found[:8]


async def wait_for_jobs(project_id: int, args: dict[str, Any]) -> dict[str, Any]:
    """Poll until the jobs finish. Does not enqueue."""
    wanted = args.get("job_ids")
    ids = [int(item) for item in wanted] if isinstance(wanted, list) else None
    timeout = int(settings.queue_poll_timeout_sec or 0)
    deadline = None if timeout == 0 else time.monotonic() + timeout
    while True:
        conn = connect()
        try:
            if ids:
                marks = ",".join("?" for _ in ids)
                rows = conn.execute(
                    f"SELECT * FROM jobs WHERE project_id = ? AND id IN ({marks})",
                    (project_id, *ids),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM jobs
                    WHERE project_id = ? AND kind IN ('image', 'video')
                      AND status IN ('pending', 'running', 'completed', 'failed', 'cancelled')
                    ORDER BY id DESC
                    LIMIT 20
                    """,
                    (project_id,),
                ).fetchall()
        finally:
            conn.close()
        jobs = [row_to_dict(row) for row in rows]
        if ids:
            pending = [job for job in jobs if job.get("status") not in _TERMINAL]
            missing = len(jobs) < len(ids)
        else:
            pending = [job for job in jobs if job.get("status") in ("pending", "running")]
            missing = False
        if jobs and not pending and not missing:
            return {
                "ok": True,
                "jobs": [
                    {
                        "id": job.get("id"),
                        "status": job.get("status"),
                        "kind": job.get("kind"),
                        "outputs": _outputs(job),
                    }
                    for job in jobs
                ],
            }
        if not jobs:
            if ids:
                return {"ok": False, "error": "jobs not found"}
            return {"ok": True, "jobs": []}
        if deadline is not None and time.monotonic() >= deadline:
            return {"ok": False, "error": "timed out waiting for jobs", "jobs": jobs}
        await asyncio.sleep(0.25)
