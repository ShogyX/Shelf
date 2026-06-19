"""Run the API server: `python -m app` (binds to SHELF_HOST:SHELF_PORT, default 0.0.0.0:8000)."""
from __future__ import annotations

import os

import uvicorn

from .config import get_settings


def _assert_single_worker(env: "os._Environ[str] | dict[str, str] | None" = None) -> None:
    """SEC-S2: Shelf keeps brute-force lockout, login rate-limiting, and request-stats counters
    IN-PROCESS, so it must run a SINGLE worker — a 2nd worker would split that state (e.g. halving
    the effective per-IP/username lockout). ``uvicorn.run`` honours WEB_CONCURRENCY when workers isn't
    pinned, so an env that scales the process out is a real footgun: refuse it loudly rather than
    silently weaken the lockout. (Running >1 worker needs that state moved to the DB first; external
    launchers like ``gunicorn --workers N`` that bypass this entry point are unsupported.)"""
    env = os.environ if env is None else env
    try:
        workers = int(env.get("WEB_CONCURRENCY") or env.get("SHELF_WORKERS") or 1)
    except ValueError:
        workers = 2  # unparseable worker count → treat as misconfigured and refuse, not crash
    if workers > 1:
        raise SystemExit(
            "Shelf runs a single worker (in-process auth-lockout / login rate-limit / request-stats "
            "state). Unset WEB_CONCURRENCY/SHELF_WORKERS or set it to 1; move that state to the DB "
            "before scaling out. (SEC-S2)"
        )


def main() -> None:
    _assert_single_worker()
    settings = get_settings()
    level = (settings.log_level or "INFO").upper()
    # Behind a trusted reverse proxy (e.g. cloudflared on localhost), honour
    # X-Forwarded-Proto/For so request.url.scheme is https and the client IP is real —
    # only from the configured proxy IPs so they can't be spoofed by direct clients.
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        proxy_headers=settings.trust_proxy,
        forwarded_allow_ips=(settings.forwarded_allow_ips if settings.trust_proxy else None),
        server_header=False,  # don't advertise the server software
        date_header=True,
        log_level=level.lower(),
        # The frontend polls Index/Jobs every few seconds while crawling — the per-request access
        # log would flood journald. Off unless explicitly debugging.
        access_log=(level == "DEBUG"),
    )


if __name__ == "__main__":
    main()
