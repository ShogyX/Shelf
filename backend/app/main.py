"""FastAPI application factory."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .auth import require_auth
from .config import get_settings
from .db import SessionLocal, init_db
from .ingestion.adapters import *  # noqa: F401,F403 (register adapters)
from .ingestion.engine import sync_all_sources
from .ingestion.scheduler import shutdown_scheduler, start_scheduler
from .ingestion.watcher import manager as folder_watcher
from .routers import (
    auth,
    chapters,
    delivery,
    health,
    index,
    jobs,
    local_folders,
    reading,
    sources,
    works,
)
from .routers import settings as settings_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    db = SessionLocal()
    try:
        sync_all_sources(db)
    finally:
        db.close()
    if settings.scheduler_enabled:
        start_scheduler()
    folder_watcher.start()
    yield
    folder_watcher.stop()
    shutdown_scheduler()


def create_app() -> FastAPI:
    app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    from fastapi import Depends

    api = "/api"
    # Open endpoints: health + auth (login/setup/me/logout). User-management routes
    # inside the auth router enforce admin themselves.
    app.include_router(health.router, prefix=api, tags=["health"])
    app.include_router(auth.router, prefix=api, tags=["auth"])
    # Everything else requires a logged-in user.
    gated = [Depends(require_auth)]
    app.include_router(works.router, prefix=api, tags=["works"], dependencies=gated)
    app.include_router(chapters.router, prefix=api, tags=["chapters"], dependencies=gated)
    app.include_router(reading.router, prefix=api, tags=["reading"], dependencies=gated)
    app.include_router(sources.router, prefix=api, tags=["sources"], dependencies=gated)
    app.include_router(jobs.router, prefix=api, tags=["jobs"], dependencies=gated)
    app.include_router(settings_router.router, prefix=api, tags=["settings"], dependencies=gated)
    app.include_router(delivery.router, prefix=api, tags=["delivery"], dependencies=gated)
    app.include_router(local_folders.router, prefix=api, tags=["local-folders"], dependencies=gated)
    app.include_router(index.router, prefix=api, tags=["index"], dependencies=gated)

    from fastapi.staticfiles import StaticFiles

    from .covers import covers_dir
    from .media import media_dir

    app.mount("/covers", StaticFiles(directory=covers_dir()), name="covers")
    app.mount("/media", StaticFiles(directory=media_dir()), name="media")
    _mount_spa(app)
    return app


def _mount_spa(app: FastAPI) -> None:
    """Serve the built frontend (SPA) so a single service hosts API + UI."""
    from pathlib import Path

    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles

    dist = (
        Path(settings.static_dir)
        if settings.static_dir
        else Path(__file__).resolve().parents[2] / "frontend" / "dist"
    )
    index = dist / "index.html"
    if not index.is_file():
        logging.getLogger("shelf").info("no built frontend at %s; serving API only", dist)
        return

    if (dist / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    @app.get("/{full_path:path}")
    async def spa(full_path: str):
        # API/docs are matched by their own routes first; anything else is the SPA.
        if full_path.startswith(("api/", "docs", "openapi.json", "redoc")):
            from fastapi import HTTPException

            raise HTTPException(404)
        candidate = dist / full_path
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(index)  # client-side routing fallback


app = create_app()
