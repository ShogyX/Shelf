"""FastAPI application factory."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .auth import require_auth
from .config import get_settings
from .db import SessionLocal, boot_recover, init_db
from .ingestion.adapters import *  # noqa: F401,F403 (register adapters)
from .ingestion.engine import sync_all_sources
from .ingestion.scheduler import shutdown_scheduler, start_scheduler
from .ingestion.watcher import manager as folder_watcher
from .routers import (
    auth,
    bookshelves,
    chapters,
    delivery,
    goodreads,
    health,
    imgproxy,
    index,
    integrations,
    jobs,
    local_folders,
    metadata,
    reading,
    sources,
    works,
)
from .routers import settings as settings_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()         # schema only (also run by read-only clients like shelfcli)
    boot_recover()    # server-only data maintenance: budget/retired-source recovery + WAL reclaim
    db = SessionLocal()
    try:
        sync_all_sources(db)
        # One-time: drain content-less reader dead-ends already queued (e.g. j-novel /read/ parts)
        # by collapsing them to their work landing, so the crawl spends requests on titles.
        from .ingestion.indexer import reclaim_reader_deadends
        reclaim_reader_deadends(db)
    finally:
        db.close()
    if settings.scheduler_enabled:
        start_scheduler()
    folder_watcher.start()
    yield
    folder_watcher.stop()
    shutdown_scheduler()


def create_app() -> FastAPI:
    # Hide the interactive API surface in production unless explicitly enabled.
    doc_kw = {} if settings.enable_docs else {"docs_url": None, "redoc_url": None,
                                              "openapi_url": None}
    app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan, **doc_kw)

    # Reject requests with an unexpected Host header (set SHELF_ALLOWED_HOSTS in prod).
    if settings.allowed_hosts and settings.allowed_hosts != ["*"]:
        from starlette.middleware.trustedhost import TrustedHostMiddleware
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)

    # Same-origin in production (SPA + API share an origin); CORS is only for the dev
    # Vite server. allow_credentials with specific origins (never "*").
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    @app.middleware("http")
    async def _security_headers(request, call_next):
        resp = await call_next(request)
        if settings.security_headers:
            h = resp.headers
            h.setdefault("X-Content-Type-Options", "nosniff")
            h.setdefault("X-Frame-Options", "DENY")
            h.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
            h.setdefault("Cross-Origin-Opener-Policy", "same-origin")
            h.setdefault("Permissions-Policy",
                         "geolocation=(), microphone=(), camera=(), interest-cohort=()")
            if settings.content_security_policy:
                h.setdefault("Content-Security-Policy", settings.content_security_policy)
            proto = request.headers.get("x-forwarded-proto", request.url.scheme)
            if settings.hsts and proto == "https":
                h.setdefault("Strict-Transport-Security",
                             "max-age=31536000; includeSubDomains")
        return resp

    from fastapi import Depends

    api = "/api"
    # Open endpoints: health + auth (login/setup/me/logout). User-management routes
    # inside the auth router enforce admin themselves.
    app.include_router(health.router, prefix=api, tags=["health"])
    app.include_router(auth.router, prefix=api, tags=["auth"])
    # Everything else requires a logged-in user.
    from .auth import require_admin

    gated = [Depends(require_auth)]
    # Infra routers that map the host filesystem / store integration credentials / trigger
    # outbound fetches are admin-only — a low-privilege account must not reconfigure them.
    admin_gated = [Depends(require_admin)]
    app.include_router(works.router, prefix=api, tags=["works"], dependencies=gated)
    app.include_router(bookshelves.router, prefix=api, tags=["bookshelves"], dependencies=gated)
    app.include_router(chapters.router, prefix=api, tags=["chapters"], dependencies=gated)
    app.include_router(reading.router, prefix=api, tags=["reading"], dependencies=gated)
    app.include_router(sources.router, prefix=api, tags=["sources"], dependencies=gated)
    app.include_router(jobs.router, prefix=api, tags=["jobs"], dependencies=gated)
    app.include_router(settings_router.router, prefix=api, tags=["settings"], dependencies=gated)
    app.include_router(delivery.router, prefix=api, tags=["delivery"], dependencies=gated)
    # Goodreads is per-user (each user connects their own shelf), so it's auth-gated, not admin —
    # unlike the operator-wide /integrations surface below.
    app.include_router(goodreads.router, prefix=api, tags=["goodreads"], dependencies=gated)
    app.include_router(local_folders.router, prefix=api, tags=["local-folders"],
                       dependencies=admin_gated)
    app.include_router(index.router, prefix=api, tags=["index"], dependencies=gated)
    # Metadata-provider ops drive outbound provider fetches + library hooks → admin-only.
    app.include_router(metadata.router, prefix=api, tags=["metadata"], dependencies=admin_gated)
    app.include_router(integrations.router, prefix=api, tags=["integrations"],
                       dependencies=admin_gated)
    app.include_router(imgproxy.router, prefix=api, tags=["imgproxy"], dependencies=gated)

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
        # Resolve + confine to dist so "../../etc/passwd" style paths can't escape.
        if full_path:
            try:
                candidate = (dist / full_path).resolve()
                candidate.relative_to(dist.resolve())
            except (ValueError, OSError):
                candidate = None
            if candidate is not None and candidate.is_file():
                return FileResponse(candidate)
        # index.html must always revalidate so a new build (new hashed assets, e.g.
        # after adding auth) is picked up instead of a cached pre-auth shell.
        return FileResponse(index, headers={"Cache-Control": "no-cache, must-revalidate"})


app = create_app()
