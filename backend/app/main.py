"""FastAPI application factory."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .auth import require_auth
from .config import get_settings
from .db import SessionLocal, apply_pending_restore, boot_recover, init_db
from .ingestion.adapters import *  # noqa: F401,F403 (register adapters)
from .ingestion.engine import sync_all_sources
from .ingestion.scheduler import shutdown_scheduler, start_scheduler
from .ingestion.watcher import manager as folder_watcher
from .routers import (
    absapi,
    auth,
    bookshelves,
    chapters,
    delivery,
    goodreads,
    health,
    imgproxy,
    index,
    integrations,
    issues,
    jobs,
    list_imports,
    local_folders,
    metadata,
    notifications,
    reading,
    sources,
    stock,
    subscriptions,
    wanted,
    works,
)
from .routers import settings as settings_router

settings = get_settings()
# Honour SHELF_LOG_LEVEL (default INFO). Configure once here; the access-log volume is tamed in
# __main__ for prod. force=True so a re-import can't leave a stale handler/level.
logging.basicConfig(
    level=getattr(logging, (settings.log_level or "INFO").upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    force=True,
)
# httpx logs every request line ("HTTP Request: GET <full-url> …") at INFO — for provider calls
# the URL carries the API key in its query string (Google Books ?key=…), so at INFO the secret is
# written to the log on every request. Quiet it to WARNING: kills the leak + the per-request noise.
logging.getLogger("httpx").setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(app: FastAPI):
    apply_pending_restore()  # BEFORE any DB use: swap in a staged full-DB snapshot restore, if any
    init_db()         # schema only (also run by read-only clients like shelfcli)
    boot_recover()    # server-only data maintenance: budget/retired-source recovery + WAL reclaim
    db = SessionLocal()
    try:
        from . import config_store, storage
        storage.load(db)        # warm admin-configured path overrides (image cache / covers / backups)
        config_store.load(db)   # warm runtime config overrides (Settings → System)
        sync_all_sources(db)
        # Seed the live fetcher with any operator-edited crawl identity (UA + contact).
        from .ingestion import operator_identity
        operator_identity.apply_saved(db)
        # One-time: drain content-less reader dead-ends already queued (e.g. j-novel /read/ parts)
        # by collapsing them to their work landing, so the crawl spends requests on titles.
        from .ingestion.indexer import reclaim_reader_deadends
        reclaim_reader_deadends(db)
    finally:
        db.close()
    if settings.scheduler_enabled:
        start_scheduler()
    folder_watcher.start()
    # systemd readiness + watchdog (O4): tell Type=notify we're up, then ping the watchdog. Both
    # are no-ops when not run under a notify-enabled unit, so this is safe everywhere.
    from . import sdnotify
    sdnotify.notify("READY=1")
    _wd_task = asyncio.create_task(sdnotify.watchdog_loop())
    # Notify admins the instance came up (off the event loop; opt-in, default off).
    if settings.scheduler_enabled:
        from . import notifications as notif
        await asyncio.to_thread(
            notif.dispatch_threadsafe, "ops.app_started",
            audience="admin", title=f"{settings.app_name} started",
            body="The application started up.", dedup_key="app_started", cooldown=60.0)
    yield
    sdnotify.notify("STOPPING=1")
    _wd_task.cancel()
    try:
        await _wd_task
    except asyncio.CancelledError:
        pass
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

    # Inbound access log (Shelf otherwise has none) — one INFO line per non-static request with its
    # status, so companion-app / API traffic is visible in journalctl. Skips the SPA + static assets to
    # stay quiet. Gated by SHELF_ACCESS_LOG=1 so it's opt-in.
    import logging as _logging
    import os as _os
    if _os.environ.get("SHELF_ACCESS_LOG") == "1":
        _alog = _logging.getLogger("shelf.access")

        @app.middleware("http")
        async def _access_log(request, call_next):
            resp = await call_next(request)
            p = request.url.path
            if not (p.startswith(("/assets", "/covers")) or p == "/"
                    or p.rsplit(".", 1)[-1] in ("js", "css", "png", "svg", "ico", "woff2", "webp", "map")):
                q = request.url.query
                pq = f"{p}?{q}" if q else p
                _alog.info("%s %s -> %s", request.method,
                           pq.replace("\n", " ").replace("\r", " "), resp.status_code)
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
    app.include_router(notifications.router, prefix=api, tags=["notifications"], dependencies=gated)
    app.include_router(delivery.router, prefix=api, tags=["delivery"], dependencies=gated)
    # Goodreads is per-user (each user connects their own shelf), so it's auth-gated, not admin —
    # unlike the operator-wide /integrations surface below.
    app.include_router(goodreads.router, prefix=api, tags=["goodreads"], dependencies=gated)
    # External reading-list imports (AniList/Goodreads/Open Library/Hardcover/MAL/Amazon) — per-user.
    app.include_router(list_imports.router, prefix=api, tags=["list-imports"], dependencies=gated)
    app.include_router(local_folders.router, prefix=api, tags=["local-folders"],
                       dependencies=admin_gated)
    app.include_router(index.router, prefix=api, tags=["index"], dependencies=gated)
    # Wanted: a user's requested titles + tracked series/authors with live acquisition state; admins
    # get an instance-wide + per-user view. Admin actions (recheck/rescan) enforce admin themselves.
    app.include_router(wanted.router, prefix=api, tags=["wanted"], dependencies=gated)
    # Following (per-user follow of an author / series): a user sees + manages only their own.
    app.include_router(subscriptions.router, prefix=api, tags=["subscriptions"], dependencies=gated)
    app.include_router(issues.router, prefix=api, tags=["issues"], dependencies=gated)
    # Audiobookshelf-compatible surface for companion apps (Still). Mounted at the ROOT (it declares
    # its own /login, /ping and /api/... paths) and self-authenticates per-endpoint via a bearer/query
    # session token, so it is deliberately NOT behind the `gated` dependency.
    app.include_router(absapi.router, tags=["absapi"])
    # Metadata-provider ops drive outbound provider fetches + library hooks → admin-only.
    app.include_router(metadata.router, prefix=api, tags=["metadata"], dependencies=admin_gated)
    app.include_router(integrations.router, prefix=api, tags=["integrations"],
                       dependencies=admin_gated)
    # Library stocking (operator pre-fetch via the usenet pipeline) → admin-only.
    app.include_router(stock.router, prefix=api, tags=["stock"], dependencies=admin_gated)
    # Backup/restore carries every credential + user → admin-only.
    from .routers import backup as backup_router
    app.include_router(backup_router.router, prefix=api, tags=["backup"],
                       dependencies=admin_gated)
    app.include_router(imgproxy.router, prefix=api, tags=["imgproxy"], dependencies=gated)

    from .config import get_settings
    from .covers import covers_dir
    from .media import media_dir
    from .static_auth import SessionStaticFiles

    # Session-gated: comic page imagery + cached chapter images (and covers) are per-user library
    # content, served only to authenticated clients — same isolation as every API route. The
    # cookie travels on the same-origin <img> requests (like /api/cover); disk-based export is
    # unaffected.
    cookie = get_settings().auth_cookie
    # /covers: long REVALIDATING cache (covers are key-addressed, so a cover can be replaced under a
    # stable key — not safe to mark immutable; cache a day then revalidate via ETag).
    app.mount("/covers", SessionStaticFiles(directory=covers_dir(), cookie_name=cookie, long_cache=True),
              name="covers")
    app.mount("/media", SessionStaticFiles(directory=media_dir(), cookie_name=cookie), name="media")
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
        # Vite emits content-hashed asset filenames (e.g. index-a1b2c3.js): a changed bundle gets a
        # NEW name, so the old one is immutable. Plain StaticFiles only sends ETag/Last-Modified, so
        # the browser revalidates every JS/CSS chunk on each load; mark them immutable to serve the
        # app shell from cache with zero network. (index.html itself stays no-cache below, so a new
        # build's new asset names are always picked up.)
        class _ImmutableStatic(StaticFiles):
            async def __call__(self, scope, receive, send):
                if scope["type"] != "http":
                    await super().__call__(scope, receive, send)
                    return

                async def send_wrap(message, _send=send):  # bind original send (avoid self-recursion)
                    if message["type"] == "http.response.start":
                        hdrs = [(k, v) for (k, v) in message.get("headers", [])
                                if k.lower() != b"cache-control"]
                        hdrs.append((b"cache-control", b"public, max-age=31536000, immutable"))
                        message = {**message, "headers": hdrs}
                    await _send(message)
                await super().__call__(scope, receive, send_wrap)

        app.mount("/assets", _ImmutableStatic(directory=dist / "assets"), name="assets")

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
