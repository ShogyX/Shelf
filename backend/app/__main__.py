"""Run the API server: `python -m app` (binds to SHELF_HOST:SHELF_PORT, default 0.0.0.0:8000)."""
from __future__ import annotations

import uvicorn

from .config import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run("app.main:app", host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
