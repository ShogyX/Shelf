"""Application settings, loaded from environment (with sane self-host defaults)."""
from __future__ import annotations

import json
from functools import lru_cache
from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SHELF_", env_file=".env", extra="ignore")

    app_name: str = "Shelf"
    database_url: str = "sqlite:///./shelf.db"

    # Root log level (SHELF_LOG_LEVEL=DEBUG|INFO|WARNING|ERROR). Per-tick INFO chatter is demoted to
    # DEBUG, so INFO stays readable under the continuous crawl; raise to DEBUG to see tick detail.
    log_level: str = "INFO"

    # Automatic scheduled backups so an unattended instance isn't left with ZERO backups. Defaults to
    # a daily "data" backup (the expensive-to-rebuild library DB, WITHOUT the huge media tree) keeping
    # the 7 newest. SHELF_AUTO_BACKUP_LEVEL=full also captures media (can be tens of GB). Set
    # SHELF_AUTO_BACKUP_ENABLED=0 to disable.
    auto_backup_enabled: bool = True
    auto_backup_level: str = "data"          # settings | data | full
    auto_backup_interval_hours: int = 24
    auto_backup_keep: int = 7

    # On-disk image cache (covers + remote chapter images) size cap; a periodic sweep LRU-evicts
    # back under this. Cached images are re-fetchable on miss, so eviction only trades disk for an
    # occasional re-download. 0 disables the cap.
    imgcache_max_mb: int = 8192

    # Network binding (overridable via SHELF_HOST / SHELF_PORT). Binding all interfaces is the
    # intended default for a self-hosted server; public deployments use tunnel mode, which sets
    # SHELF_HOST=127.0.0.1 (see deploy/ + README "Exposing it on the internet").
    host: str = "0.0.0.0"  # nosec B104 — deliberate default; harden via tunnel mode / SHELF_HOST
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
    # After this many consecutive pages that surface no NEW catalog TITLE, the crawl stops
    # DISCOVERING more pages — but it still drains whatever's queued, so a crawl only truly
    # finishes when its frontier is empty (no content is abandoned). Finding more links (e.g. a
    # novel site's endless pagination) does NOT reset this bound — only a new title does. Editable
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
    allowed_hosts: Annotated[list[str], NoDecode] = ["*"]
    # Absolute public origin (e.g. https://shelf.example) used to build links in EMAILS (password
    # reset). MUST be set before exposing password reset, or the link host would otherwise be taken
    # from the untrusted Host header (reset-poisoning). Blank → fall back to allowed_hosts; if that's
    # also unrestricted ("*"), no reset email is sent (the operator must configure one of these).
    public_base_url: str = ""
    # Brute-force protection on login (per username + per client IP).
    login_max_attempts: int = 6
    login_window_seconds: int = 900       # 15 min sliding window / lockout
    min_password_length: int = 8
    # Self-registration gate: "closed" (only admins create users — the default/historical behavior),
    # "open" (self-signup → active + logged in immediately), "approval" (self-signup → pending until
    # an admin approves). Runtime-editable via Settings → System (config_store).
    registration_mode: str = "closed"
    # Missing-content ledger: how long an unavailable title waits before the periodic re-check tick
    # tries to acquire it again (jittered ±25% so a batch marked unavailable together doesn't all come
    # due at once), and how many due titles a single re-check tick re-acquires (flood control —
    # combined with the ~30-min cadence + the jitter this bounds re-check request volume).
    missing_recheck_days: int = 14
    missing_recheck_batch: int = 8
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

    # Cloudflare challenge solver (FlareSolverr-compatible proxy, e.g. http://10.10.102.23:8191).
    # When set, any request that hits a Cloudflare interstitial/Turnstile challenge is routed through
    # it: the solver drives a real browser to obtain a cf_clearance cookie + matching User-Agent,
    # which the app then caches per host and REPLAYS on cheap plain-HTTP requests until it expires
    # (only re-solving on the next challenge). Empty = disabled (falls back to the in-app headless
    # renderer). Applies to the JSON-API catalog crawlers AND the HTML PoliteFetcher.
    flaresolverr_url: str = ""
    flaresolverr_timeout_s: int = 60        # max seconds we let the solver work one challenge
    flaresolverr_clearance_ttl_s: int = 1500  # reuse a solved cf_clearance this long (~25 min)

    # comix.to browser crawler. comix fronts its catalog with a Cloudflare Turnstile challenge AND a
    # per-request signed-token API, so it can only be read with a real evasion-hardened browser
    # (zendriver) that passes the challenge and pages the server-rendered /browse grid. Enabled by
    # default; needs zendriver + an X server (Xvfb) + a Chromium binary. Disable to skip comix.
    comix_browser_enabled: bool = True
    comix_browser_pages_per_tick: int = 10   # browse pages crawled per tick (28 titles each)
    # Chromium for the solver/crawler. Empty → auto-detect the bundled Playwright build. Exposed to
    # the standalone crawler subprocess as SHELF_SOLVER_CHROME_PATH.
    solver_chrome_path: str = ""

    # Hard cap on total simultaneous in-flight HTTP fetches across ALL crawls. Each index site
    # and backfill job runs concurrently with its OWN per-domain/per-source rate budget (which is
    # what enforces politeness per target); this is just a machine-resource backstop, so it's set
    # generously — independent crawls shouldn't queue behind each other for a slot. Decoupled from
    # the per-tick batch size ("parallel_fetches" tuning), which sizes per-site/per-job work.
    global_max_concurrency: int = 16
    default_min_request_interval_s: float = 5.0

    # Slow-crawl scheduler.
    scheduler_enabled: bool = True
    scheduler_tick_seconds: int = 15
    chapters_per_tick: int = 1

    # CORS for the Vite dev server.
    cors_origins: Annotated[list[str], NoDecode] = ["http://localhost:5173", "http://127.0.0.1:5173"]

    @field_validator("allowed_hosts", "cors_origins", mode="before")
    @classmethod
    def _parse_list_env(cls, v):
        """Accept a JSON array OR a plain comma-separated string for these list env vars (e.g.
        SHELF_ALLOWED_HOSTS=example.com or "a.com,b.com"). NoDecode disables pydantic-settings' JSON
        decode (which would reject the bare/CSV form with a SettingsError), so we parse both here."""
        if isinstance(v, str):
            s = v.strip()
            if not s:
                return []
            if s[0] in "[\"":          # looks like JSON (array or quoted) → decode it
                try:
                    parsed = json.loads(s)
                    return parsed if isinstance(parsed, list) else [parsed]
                except ValueError:
                    pass
            return [p.strip() for p in s.split(",") if p.strip()]
        return v

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
