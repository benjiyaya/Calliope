from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from calliope.config import settings
from calliope.db import get_db, migrate_db, rebase_stale_asset_paths
from calliope.queue.worker import queue_worker
from calliope.routers import (
    agent,
    assets,
    events,
    jobs,
    playground,
    projects,
    scenes,
    settings as settings_router,
    story,
    workflows,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("calliope")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.assets_dir.mkdir(parents=True, exist_ok=True)
    await migrate_db(settings.db_path)
    # Folder moved? Stored asset paths still point at the old install root —
    # rebase them onto the current data dir before serving anything.
    conn = get_db(settings.db_path)
    try:
        rebased = rebase_stale_asset_paths(conn, settings.data_dir, settings.assets_dir)
        if rebased:
            conn.commit()
            logger.info(
                "Rebased %s asset path(s) from a previous install location onto %s",
                rebased,
                settings.data_dir,
            )
    finally:
        conn.close()
    await queue_worker.start()
    logger.info("Calliope started — db=%s dry_run=%s", settings.db_path, settings.dry_run)
    yield
    from calliope.agent.harness.runner import runner

    await runner.shutdown()
    await queue_worker.stop()
    logger.info("Calliope shutting down")


def create_app() -> FastAPI:
    app = FastAPI(title="Calliope", version="1.2.0", lifespan=lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(projects.router, prefix="/api/projects", tags=["projects"])
    app.include_router(settings_router.router, prefix="/api/settings", tags=["settings"])
    app.include_router(story.router, prefix="/api/projects", tags=["story"])
    app.include_router(scenes.router, prefix="/api/projects", tags=["scenes"])
    app.include_router(assets.router, prefix="/api", tags=["assets"])
    app.include_router(workflows.router, prefix="/api/workflows", tags=["workflows"])
    app.include_router(jobs.router, prefix="/api/jobs", tags=["jobs"])
    app.include_router(playground.router, prefix="/api/playground", tags=["playground"])
    app.include_router(agent.router, prefix="/api/agent", tags=["agent"])
    app.include_router(events.router, prefix="/api/events", tags=["events"])

    @app.get("/api/health")
    async def health() -> dict:
        return {"status": "ok", "version": "1.2.0", "dry_run": settings.dry_run}

    # Catch unknown /api/* before StaticFiles — otherwise POST falls through and
    # returns a confusing 405 Method Not Allowed from the file server.
    @app.api_route(
        "/api/{full_path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
        include_in_schema=False,
    )
    async def api_not_found(full_path: str) -> None:
        raise HTTPException(status_code=404, detail=f"API route not found: /api/{full_path}")

    static_dir = Path(__file__).resolve().parent / "static"
    if static_dir.exists():
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")

    return app


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Calliope backend server")
    parser.add_argument("--host", default=None, help="Bind host (default: from config)")
    parser.add_argument("--port", type=int, default=None, help="Bind port (default: from config)")
    args = parser.parse_args()

    host = args.host or settings.host
    port = args.port or settings.port

    app = create_app()
    uvicorn = __import__("uvicorn")
    uvicorn.run(app, host=host, port=port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
