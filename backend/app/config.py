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

    # Where instance backups (.zip) are stored so they appear as selectable objects in the
    # Backups tab — both app-created and uploaded ones. MUST live outside media_dir (a full
    # backup walks media_dir; nesting backups inside it would recurse). Defaults to ../backups.
    backup_dir: str = ""

    # URL-index auto-crawl bounds. Pages are UNLIMITED (0 = no cap); a crawl instead stops
    # on the idle threshold below. max_depth stays as a loose structural bound.
    index_max_pages: int = 0  # 0 = unlimited
    # Depth is a loop guard (URLs are de-duped), NOT a coverage limit — keep it loose so deep
    # pagination and nested sections of an unlimited crawl are still reached (8 was far too
    # shallow: it cut paginated listings off after ~8 "next page" hops). Applied as a floor for
    # unlimited crawls (see indexer._enqueue_links).
    index_max_depth: int = 50
    # After this many consecutive pages with NOTHING new (no catalog title AND no new link), the
    # crawl stops DISCOVERING more pages — but it still drains whatever's already queued, so a
    # crawl only truly finishes when its frontier is empty (no content is abandoned). Editable
    # globally (Settings → Indexing) and per-site (Jobs page).
    index_stop_after_idle_pages: int = 200
    # Cap how far the pending frontier may run ahead of what's been fetched. Generous so a rich
    # site's links aren't dropped for lack of room (dropped links may never be re-seen — a single
    # hub page can list 500+ works, and the old 500 cap silently truncated such catalogs), while
    # still bounding a runaway crawl ahead of the slower per-page ingestion. Termination is governed
    # by the idle-stop, not this cap, so it's a safety ceiling — set well above any real catalog.
    index_max_pending_frontier: int = 50000

    # Authentication / sessions.
    auth_cookie: str = "shelf_session"
    session_days: int = 30
    # Set true only when served over HTTPS (else the cookie won't be sent over plain HTTP).
    # When trust_proxy is on we also auto-enable Secure for requests forwarded as https.
    cookie_secure: bool = False
    cookie_samesite: str = "lax"  # lax | strict | none
    # User shelfcli writes reading progress as (username); defaults to the first admin.
    cli_user: str = ""

    # --- Hardening (for internet exposure, e.g. behind a Cloudflare tunnel) ----
    # Trust X-Forwarded-* / CF-Connecting-IP (ONLY enable when behind a trusted proxy
    # such as cloudflared bound to localhost; otherwise clients could spoof them).
    trust_proxy: bool = False
    # IPs allowed to set forwarded headers (the local cloudflared connection).
    forwarded_allow_ips: str = "127.0.0.1"
    # Restrict the Host header to these names ("*" = any). Set to your domain in prod.
    allowed_hosts: list[str] = ["*"]
    # Brute-force protection on login (per username + per client IP).
    login_max_attempts: int = 6
    login_window_seconds: int = 900       # 15 min sliding window / lockout
    min_password_length: int = 8
    # Optional shared secret required to create the first admin (POST /auth/setup).
    # Set this before exposing the app so an attacker can't claim the admin account.
    setup_token: str = ""
    # Expose the interactive API docs (/docs, /openapi.json). Off by default in prod.
    enable_docs: bool = False
    # Security response headers.
    security_headers: bool = True
    hsts: bool = True                     # only emitted over https
    content_security_policy: str = (
        "default-src 'self'; img-src 'self' https: data:; style-src 'self' 'unsafe-inline'; "
        "script-src 'self'; connect-src 'self'; font-src 'self' data:; "
        "frame-ancestors 'none'; base-uri 'self'; form-action 'self'; object-src 'none'"
    )

    # PoliteFetcher identity. Default to a generic, current Chrome User-Agent so sources that block
    # non-browser agents serve the crawler normally (overridable in Settings → Indexing).
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    )
    contact_email: str = "operator@localhost"

    # Hard cap on total simultaneous in-flight HTTP fetches across ALL crawls. Each index site
    # and backfill job runs concurrently with its OWN per-domain/per-source rate budget (which is
    # what enforces politeness per target); this is just a machine-resource backstop, so it's set
    # generously — independent crawls shouldn't queue behind each other for a slot. Decoupled from
    # the per-tick batch size ("parallel_fetches" tuning), which sizes per-site/per-job work.
    global_max_concurrency: int = 16
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
