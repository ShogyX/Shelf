"""The authoritative catalog of every integration the operator can connect.

One entry per ``kind`` describes what it is, what it contributes, how Shelf matches with it, and
its **request-limit defaults** (per-minute cap + timeout) chosen to avoid provider rate-blocks and
timeouts out of the box. The Settings UI reads this (``GET /integrations/catalog``) to render the
provider boxes + help text, and the HTTP clients read the limits (:func:`resolve_limits`) to throttle
outbound calls — so descriptions, defaults, and enforcement all share one source of truth.
"""
from __future__ import annotations

# category: "metadata" (enrich hooked works) | "manager" (sync a library) | "pipeline" (acquire)
# auth:     "none" | "optional_key" | "key" | "token" | "cookie"
# rpm / timeout: default request cap (requests per minute) + per-request timeout (seconds).
PROVIDER_CATALOG: list[dict] = [
    # --------------------------------------------------------------- metadata
    {
        "kind": "ranobedb", "category": "metadata", "label": "RanobeDB",
        "tagline": "Light-novel metadata & volumes",
        "provides": ["author", "synopsis", "cover", "volume count", "related series"],
        "use": "The canonical source for light novels: pulls clean title/author/synopsis/cover, "
               "counts volumes, and surfaces related series (prequels/sequels/spin-offs).",
        "requests": "Keyless public API (ranobedb.org). Watches series for new volumes, so it's "
                    "re-checked on the release sweep.",
        "matching": "Matched to your hooked light novels by title + author; a volume/series gate "
                    "keeps it from matching a manga adaptation.",
        "auth": "none", "per_user": False, "rpm": 30, "timeout": 20.0,
    },
    {
        "kind": "googlebooks", "category": "metadata", "label": "Google Books",
        "tagline": "Broad book metadata",
        "provides": ["author", "synopsis", "cover", "page count", "categories"],
        "use": "Wide coverage of prose fiction (and many comics) — the best fallback when a title "
               "isn't a light novel. Provides author, synopsis, high-res cover and page count.",
        "requests": "Public API; an API key is optional and only raises the daily quota. Editions "
                    "are static, so it isn't re-polled for releases.",
        "matching": "Matched by title + author. Requires author corroboration before accepting a "
                    "match, so common titles don't collide.",
        "auth": "optional_key", "per_user": False, "rpm": 60, "timeout": 20.0,
    },
    {
        "kind": "hardcover", "category": "metadata", "label": "Hardcover",
        "tagline": "Community book database",
        "provides": ["author", "synopsis", "cover", "extra titles"],
        "use": "A community-curated books database with strong coverage of titles Google Books and "
               "Open Library miss — used for discovery and resolution.",
        "requests": "GraphQL API; needs a personal Bearer token from your Hardcover account "
                    "(Settings → Hardcover API). Documented ~60 requests/min.",
        "matching": "Matched by title + author, with the same author gate as Google Books.",
        "auth": "token", "per_user": False, "rpm": 60, "timeout": 20.0,
    },
    {
        "kind": "anilist", "category": "metadata", "label": "AniList",
        "tagline": "Manga / manhwa chapter counts",
        "provides": ["chapter count", "genres & tags", "popularity", "cover"],
        "use": "The source of truth for how many chapters a manga / manhua / manhwa has — Shelf "
               "compares it to what you've downloaded and pulls the missing chapters. Also feeds "
               "genres, tags and popularity for discovery ranking.",
        "requests": "Keyless GraphQL API (~90 req/min upstream). Re-checked on the release sweep "
                    "as chapter counts grow.",
        "matching": "Matched by title with a media-kind gate, so a comic's count never overrides a "
                    "prose novel's.",
        "auth": "none", "per_user": False, "rpm": 60, "timeout": 15.0,
    },
    {
        "kind": "novelupdates", "category": "metadata", "label": "NovelUpdates",
        "tagline": "Web-novel chapter counts",
        "provides": ["chapter count", "status", "synopsis"],
        "use": "The authoritative chapter count + completion status for translated web novels "
               "(Chinese / Korean / Japanese).",
        "requests": "Scraped from novelupdates.com behind a Cloudflare challenge — paste a "
                    "cf_clearance cookie + matching User-Agent, or it falls back to a slow headless "
                    "render. Kept to a low rate to avoid getting blocked.",
        "matching": "Matched by title + author; a media gate keeps it to prose web novels.",
        "auth": "cookie", "per_user": False, "rpm": 6, "timeout": 30.0,
    },
    {
        "kind": "goodreads", "category": "metadata", "label": "Goodreads",
        "tagline": "Per-user want-to-read import",
        "provides": ["wishlist import"],
        "use": "Imports your Goodreads shelf as queued auto-hooks. This one is PER-USER — each user "
               "connects their own shelf from Settings → Goodreads, not here.",
        "requests": "Public RSS feed; the shelf must be public.",
        "matching": "Each shelf item is queued and auto-hooked once a matching title is discovered.",
        "auth": "none", "per_user": True, "rpm": 20, "timeout": 20.0,
    },
    # ---------------------------------------------------------------- managers
    {
        "kind": "readarr", "category": "manager", "label": "Readarr",
        "tagline": "Books / novels library manager",
        "provides": ["library sync", "grab → download"],
        "use": "Pulls your Readarr book library into the Shelf catalog and can grab new books "
               "through Readarr's own download chain.",
        "requests": "Local Readarr API (needs base URL + API key). Synced periodically.",
        "matching": "Library items become catalog entries; folder watching imports the files.",
        "auth": "key", "per_user": False, "rpm": 60, "timeout": 30.0,
    },
    {
        "kind": "kapowarr", "category": "manager", "label": "Kapowarr",
        "tagline": "Comics library manager",
        "provides": ["library sync", "grab → download"],
        "use": "Pulls your Kapowarr comic library into the Shelf catalog and can grab new volumes.",
        "requests": "Local Kapowarr API (needs base URL + API key). Synced periodically.",
        "matching": "Library items become catalog entries; folder watching imports the files.",
        "auth": "key", "per_user": False, "rpm": 60, "timeout": 30.0,
    },
    # ---------------------------------------------------------------- pipeline
    {
        "kind": "prowlarr", "category": "pipeline", "label": "Prowlarr",
        "tagline": "Indexer search (usenet)",
        "provides": ["release search", "ranked candidates"],
        "use": "Searches your enabled usenet indexers for a title; the matching engine ranks "
               "releases by format / language / edition + your preference rules. Handles both books "
               "(ebook categories) and comics/manga (category 7030, CBZ/CBR) — set comic categories "
               "+ indexers if yours differ.",
        "requests": "Local Prowlarr API (needs base URL + API key). Queried on demand when you "
                    "acquire a title; searches can be slow, so the timeout is generous.",
        "matching": "Releases are scored against the book's title / author / language and your "
                    "required / ignored / preferred terms. Comics search comic categories for CBZ/CBR.",
        "auth": "key", "per_user": False, "rpm": 60, "timeout": 30.0,
    },
    {
        "kind": "sabnzbd", "category": "pipeline", "label": "SABnzbd",
        "tagline": "Usenet downloader",
        "provides": ["download", "content verify → import"],
        "use": "Enqueues NZB downloads, then Shelf verifies the file matches the requested book "
               "before promoting it into your library path.",
        "requests": "Local SABnzbd API (needs base URL + API key). Polled while downloads run.",
        "matching": "Drives the download + import side of the acquisition pipeline.",
        "auth": "key", "per_user": False, "rpm": 120, "timeout": 30.0,
    },
    {
        "kind": "libgen", "category": "pipeline", "label": "Open libraries (LibGen)",
        "tagline": "Fallback direct download (LibGen / Anna's / …)",
        "provides": ["search", "direct download", "content verify → import"],
        "use": "A FALLBACK to the usenet pipeline: when Prowlarr/SABnzbd finds no match (or isn't "
               "installed), Shelf searches free open-library mirrors, downloads the best match, "
               "verifies it's the right book, and imports it. No account needed.",
        "requests": "Direct HTTP to the mirror sites, rate-limited per host (min interval + daily "
                    "cap + concurrency + backoff). Cloudflare-fronted sites use the headless browser.",
        "matching": "Title/author ranked, then the same content-verification gate as the usenet path.",
        "auth": "none", "per_user": False, "rpm": 30, "timeout": 45.0,
    },
]

_BY_KIND: dict[str, dict] = {p["kind"]: p for p in PROVIDER_CATALOG}

# Built-in book-catalog sources that aren't connectable integrations (shown for context only).
BUILTIN_SOURCES = [
    {"label": "Open Library", "note": "built-in · keyless", "kind": None},
]


def catalog_entry(kind: str) -> dict | None:
    return _BY_KIND.get(kind)


def category_for(kind: str) -> str:
    e = _BY_KIND.get(kind)
    return e["category"] if e else "manager"


# Request-limit bounds (defensive clamps for operator-entered values).
_RPM_MIN, _RPM_MAX = 1.0, 600.0
_TIMEOUT_MIN, _TIMEOUT_MAX = 3.0, 120.0


def resolve_limits(kind: str, config: dict | None) -> tuple[float, float]:
    """Return the (requests_per_minute, timeout_seconds) for a kind: the operator's per-integration
    override from ``config`` if set, else the catalog default. Clamped to sane bounds so a bad value
    can't disable throttling or hang a request."""
    entry = _BY_KIND.get(kind) or {}
    cfg = config or {}
    rpm = cfg.get("requests_per_minute")
    timeout = cfg.get("timeout")
    rpm = float(rpm) if isinstance(rpm, (int, float)) and rpm > 0 else float(entry.get("rpm", 60))
    timeout = (float(timeout) if isinstance(timeout, (int, float)) and timeout > 0
               else float(entry.get("timeout", 20.0)))
    rpm = max(_RPM_MIN, min(_RPM_MAX, rpm))
    timeout = max(_TIMEOUT_MIN, min(_TIMEOUT_MAX, timeout))
    return rpm, timeout
