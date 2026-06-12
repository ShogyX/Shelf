"""Run the API server: `python -m app` (binds to SHELF_HOST:SHELF_PORT, default 0.0.0.0:8000)."""
from __future__ import annotations

import uvicorn

from .config import get_settings


def main() -> None:
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
