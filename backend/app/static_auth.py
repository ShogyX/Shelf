"""Session-gated StaticFiles for /media and /covers.

The cached chapter images + comic PAGE images under media/ (and cover art under covers/) are the
same per-user library content the API gates on every route — but the raw StaticFiles mounts served
them to any unauthenticated client, leaking the whole library's imagery on a tunnel-exposed
instance. This subclass requires a valid session COOKIE before serving.

Cookie (not header) auth is correct here: every consumer is a same-origin ``<img src>`` (reader,
covers, comic pages), so the httpOnly session cookie travels automatically — exactly like the
already-gated /api/cover proxy. EPUB/Kindle export reads these files from DISK (not over HTTP), so
export and email delivery are unaffected.
"""
from __future__ import annotations

import time

from fastapi.staticfiles import StaticFiles
from starlette.requests import Request
from starlette.responses import PlainTextResponse

# A comic page loads dozens of images at once; validating the session against the DB for every one
# would multiply SQLite load under the crawl's write contention. Cache a token's validity briefly —
# IMAGE serving may lag a revocation by at most this long (the API/session itself revokes
# immediately; stale image bytes for a few seconds are harmless).
_TTL_S = 15.0
_cache: dict[str, float] = {}   # token -> monotonic expiry of the positive result


def invalidate(*tokens: str | None) -> None:
    """Evict tokens from the positive-result cache so a revoked/logged-out/password-changed session
    stops serving /media + /covers immediately (AUTHZ-2), instead of lagging by up to ``_TTL_S``."""
    for token in tokens:
        if token:
            _cache.pop(token, None)


def _token_valid(token: str | None) -> bool:
    if not token:
        return False
    now = time.monotonic()
    hit = _cache.get(token)
    if hit is not None and hit > now:
        return True
    from .auth import session_user
    from .db import SessionLocal
    db = SessionLocal()
    try:
        ok = session_user(db, token) is not None
    finally:
        db.close()
    if ok:
        _cache[token] = now + _TTL_S
        if len(_cache) > 4096:                       # bound the cache (stale entries are cheap)
            for k in [k for k, exp in _cache.items() if exp <= now]:
                _cache.pop(k, None)
    else:
        _cache.pop(token, None)
    return ok


def _with_cache_header(send, value: bytes):
    """Wrap an ASGI ``send`` so the response START carries a fixed Cache-Control (replacing any
    default StaticFiles set). Leaves 304/other messages untouched."""
    async def wrapped(message):
        if message["type"] == "http.response.start":
            headers = [(k, v) for (k, v) in message.get("headers", [])
                       if k.lower() != b"cache-control"]
            headers.append((b"cache-control", value))
            message = {**message, "headers": headers}
        await send(message)
    return wrapped


class SessionStaticFiles(StaticFiles):
    """StaticFiles that 401s unless the request carries a valid session cookie."""

    def __init__(self, *args, cookie_name: str, immutable: bool = False, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._cookie_name = cookie_name
        # This whole mount serves content-addressed (hash-named) files that never change (the /covers
        # mount). A changed cover gets a NEW hash → new URL, so the old file is safe to cache forever.
        self._immutable = immutable

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http":
            token = Request(scope).cookies.get(self._cookie_name)
            if not _token_valid(token):
                await PlainTextResponse("Not authenticated", status_code=401)(scope, receive, send)
                return
            # Content-addressed files never change → mark immutable so the browser serves repeats with
            # ZERO network (StaticFiles otherwise only sends ETag/Last-Modified, forcing a conditional
            # GET — a round-trip + a session check — for EVERY cover on EVERY page load).
            if self._immutable:
                # An all-hash-named mount (/covers): session-gated → PRIVATE (a shared proxy must not
                # serve another user's library imagery), immutable for a year.
                send = _with_cache_header(send, b"private, max-age=31536000, immutable")
            elif "/imgcache/" in scope.get("path", ""):
                send = _with_cache_header(send, b"public, max-age=31536000, immutable")
            else:
                # Other /media (non-hash-named: audio cache, etc.) — keep a modest private TTL.
                send = _with_cache_header(send, b"private, max-age=3600")
        await super().__call__(scope, receive, send)
