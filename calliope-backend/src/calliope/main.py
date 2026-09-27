from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response
from starlette.types import Scope

from calliope import __version__
from calliope.config import settings
from calliope.db import get_db, migrate_db, rebase_stale_asset_paths
from calliope.queue.worker import queue_worker
from calliope.routers import (
    agent,
    assets,
    canvas,
    events,
    jobs,
    library,
    playground,
    projects,
    scenes,
    settings as settings_router,
    shots,
    story,
    workflows,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("calliope")

# Hashed SPA assets can be cached; HTML and unknown client routes must not, or
# Electron will keep showing a previous install's index.html from 127.0.0.1.
_IMMUTABLE_SUFFIXES = {
    ".js",
    ".css",
    ".map",
    ".png",
    ".jpg",
    ".jpeg",
    ".svg",
    ".webp",
    ".gif",
    ".ico",
    ".woff",
    ".woff2",
    ".ttf",
    ".json",
}


class SPAStaticFiles(StaticFiles):
    """Serve SvelteKit's fallback index.html for client routes like /project/12.

    Starlette StaticFiles(html=True) only maps directories to index.html, not
    unknown paths. Without this, the packaged app returns FastAPI JSON
    ``{"detail":"Not Found"}`` after Create Project (a full navigation to
    ``/project/{id}``).
    """

    async def get_response(self, path: str, scope: Scope) -> Response:
        fallback = False
        try:
            response = await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code != 404:
                raise
            suffix = Path(path).suffix.lower()
            if suffix in _IMMUTABLE_SUFFIXES:
                raise
            fallback = True
            response = await super().get_response("index.html", scope)
        if fallback or path.endswith(".html") or path in {"", ".", "index.html"}:
            response.headers["Cache-Control"] = "no-store"
        return response


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
    from calliope.agent.harness.runner import runner

    runner.recover_orphaned_sessions()
    await queue_worker.start()
    logger.info("Calliope started — db=%s dry_run=%s", settings.db_path, settings.dry_run)
    # The MCP endpoint's session manager needs its task group for the app's
    # whole lifetime (see calliope.mcp_server).
    async with app.state.mcp_manager.run():
        yield
    from calliope.agent.harness.runner import runner

    await runner.shutdown()
    await queue_worker.stop()
    logger.info("Calliope shutting down")


def create_app(static_dir: Path | None = None) -> FastAPI:
    app = FastAPI(title="Calliope", version=__version__, lifespan=lifespan)

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
    app.include_router(library.router, prefix="/api/library", tags=["library"])
    app.include_router(canvas.router, prefix="/api/canvas", tags=["canvas"])
    app.include_router(shots.router, prefix="/api/shots", tags=["shots"])
    app.include_router(agent.router, prefix="/api/agent", tags=["agent"])
    app.include_router(events.router, prefix="/api/events", tags=["events"])

    @app.get("/api/health")
    async def health() -> dict:
        return {"status": "ok", "version": __version__, "dry_run": settings.dry_run}

    # Catch unknown /api/* before StaticFiles — otherwise POST falls through and
    # returns a confusing 405 Method Not Allowed from the file server.
    @app.api_route(
        "/api/{full_path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
        include_in_schema=False,
    )
    async def api_not_found(full_path: str) -> None:
        raise HTTPException(status_code=404, detail=f"API route not found: /api/{full_path}")

    # MCP server for Claude Code & co. Registered before the SPA mount, which
    # would otherwise swallow /mcp. Tools run in this process, so the web app
    # sees every change live on the event bus.
    from starlette.routing import Route

    from calliope.mcp_server import MCPEndpoint, build_session_manager

    app.state.mcp_manager = build_session_manager()
    app.router.routes.append(
        Route("/mcp", endpoint=MCPEndpoint(app.state.mcp_manager), methods=["GET", "POST", "DELETE"])
    )

    resolved_static = static_dir if static_dir is not None else Path(__file__).resolve().parent / "static"
    if resolved_static.exists():
        app.mount("/", SPAStaticFiles(directory=resolved_static, html=True), name="static")

    return app


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Calliope backend server")
    parser.add_argument("--host", default=None, help="Bind host (default: from config)")
    parser.add_argument("--port", type=int, default=None, help="Bind port (default: from config)")
    parser.add_argument(
        "--reload",
        action="store_true",
        help="Dev only: restart the server when a .py file under calliope/ changes",
    )
    args = parser.parse_args()

    host = args.host or settings.host
    port = args.port or settings.port

    if args.reload and not getattr(sys, "frozen", False):
        # Not uvicorn's --reload: on Windows it restarts the server by sending
        # CTRL_C_EVENT, which never arrives without an interactive console
        # (start-bg.bat, IDE runners) — the old process kept serving stale
        # code. watchfiles stops the child with os.kill(SIGINT), which on
        # Windows is TerminateProcess: reliable, but skips lifespan shutdown.
        # Startup recovers what that leaves behind (stale running jobs →
        # pending, orphaned agent sessions → idle) — so a restart mid-render
        # re-queues that job.
        from watchfiles import PythonFilter, run_process

        src_dir = Path(__file__).resolve().parent
        logger.info("Dev reload: watching %s", src_dir)
        run_process(
            src_dir,
            target=_serve,
            args=(host, port),
            watch_filter=PythonFilter(),
            callback=lambda changes: logger.info(
                "Code changed (%s) — restarting backend",
                ", ".join(sorted(Path(p).name for _, p in changes)),
            ),
        )
        return 0

    _serve(host, port)
    return 0


def _serve(host: str, port: int) -> None:
    """Build the app and run uvicorn (module-level so reload can spawn it)."""
    import uvicorn

    uvicorn.run(create_app(), host=host, port=port)


if __name__ == "__main__":
    sys.exit(main())
