"""Application settings, loaded from environment (with sane self-host defaults)."""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SHELF_", env_file=".env", extra="ignore")

    app_name: str = "Shelf"
    database_url: str = "sqlite:///./shelf.db"

    # Network binding (overridable via SHELF_HOST / SHELF_PORT).
    host: str = "0.0.0.0"
    port: int = 8000

    # Built frontend to serve as a SPA (set empty to disable). Defaults to ../frontend/dist.
    static_dir: str = ""

    # Where extracted cover images are written + served from (/covers/...).
    covers_dir: str = ""

    # Where extracted comic page images are written + served from (/media/...).
    media_dir: str = ""

    # URL-index auto-crawl bounds (operator-overridable per site at index time).
    index_max_pages: int = 200
    index_max_depth: int = 3

    # Authentication / sessions.
    auth_cookie: str = "shelf_session"
    session_days: int = 30
    # Set true only when served over HTTPS (else the cookie won't be sent over plain HTTP).
    cookie_secure: bool = False
    # User shelfcli writes reading progress as (username); defaults to the first admin.
    cli_user: str = ""

    # PoliteFetcher identity — honest UA + contact, per the sourcing principle.
    user_agent: str = (
        "ShelfReader/0.1 (+https://github.com/self-hosted/shelf; polite-self-host-ingester)"
    )
    contact_email: str = "operator@localhost"

    # Global politeness ceilings (per-source values may be stricter, never looser).
    global_max_concurrency: int = 2
    default_min_request_interval_s: float = 5.0
    default_max_daily_requests: int = 500

    # Slow-crawl scheduler.
    scheduler_enabled: bool = True
    scheduler_tick_seconds: int = 15
    chapters_per_tick: int = 1

    # CORS for the Vite dev server.
    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]

    # SMTP for "Send to Kindle" (the From address must be on your Amazon approved
    # personal-document sender list). Leave smtp_host empty to disable sending.
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_starttls: bool = True
    smtp_ssl: bool = False


@lru_cache
def get_settings() -> Settings:
    return Settings()
