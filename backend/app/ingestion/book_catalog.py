"""Hybrid book catalog — a persistent 'hot set' of popular books plus a live, closeness-gated
resolver against book metadata APIs (Google Books + Open Library).

Rather than mirroring millions of rows, we keep a curated hot set (seeded from Open Library
trending + popular subjects across both providers, ranked by a real audience signal) and resolve
anything else on demand: a search first checks how closely the local catalog already matches the
query; only if nothing is close enough do we hit the APIs, then cache + persist the results so the
next search is local.

Book rows are ordinary :class:`CatalogWork` rows (provider ``googlebooks`` / ``openlibrary``,
no integration), so the existing regroup tick clusters them with any web-index source carrying the
same title — one logical work, many acquisition routes.
"""
from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import httpx
from .. import telemetry
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from ..models import AppSetting, CatalogWork, Integration, Work
from .extract import authors_compatible, norm_title

log = logging.getLogger("shelf.book_catalog")

# Serialize hot-set seeding across the scheduled tick and the manual /catalog/book-sync endpoint
# (both run in this process) so two runs can't read-modify-write the cursor and lose progress.
_sync_lock = threading.Lock()

# Per-query live-resolve guard: a normalized query is resolved against the APIs at most once per
# window. Kept in a dedicated time-pruned dict (NOT the shared LRU read-cache, whose 512-entry
# eviction would re-open the window early under varied search load).
_resolve_seen: dict[str, float] = {}
_RESOLVE_SEEN_MAX = 5000

# Serialize the long-tail metadata-backfill tick (covers + series) so two runs don't double-work.
_backfill_lock = threading.Lock()

# Study-guide / summary spam that Open Library's loose search surfaces — never the real book.
_JUNK_TITLE_RE = re.compile(
    r"^\s*(summary|study guide|a guide to|workbook|analysis|conversation starters|"
    r"summary and analysis|key takeaways)\b", re.I,
)

GOOGLE_BOOKS_API = "https://www.googleapis.com/books/v1"
OPENLIBRARY = "https://openlibrary.org"
_UA = "Mozilla/5.0 (compatible; ShelfReader/0.1)"
_TIMEOUT = 20.0

BOOK_PROVIDERS = ("googlebooks", "openlibrary", "hardcover")
_DOMAIN = {"googlebooks": "books.google.com", "openlibrary": "openlibrary.org",
           "hardcover": "hardcover.app"}

# Config (AppSetting). Defaults chosen to keep the DB small + API usage polite.
_CONFIG_KEY = "book_catalog_config"
_STATE_KEY = "book_hot_set_state"
_DEFAULTS = {
    "enabled": True,
    "hot_set_cap": 20000,        # stop seeding new subject rows past this many book rows
    "closeness_threshold": 0.7,  # below this, a search falls through to the live APIs
}

# Hot-set seeding plan.
_TRENDING = ("yearly", "weekly")
_SUBJECTS = [
    "fiction", "fantasy", "science_fiction", "romance", "mystery", "thriller",
    "horror", "historical_fiction", "young_adult_fiction", "literary_fiction",
    "adventure", "crime", "biography", "graphic_novels", "poetry", "humor",
]
_PAGE = 50           # rows per subject page
_SUBJECT_DEPTH = 200  # MINIMUM per-subject pagination depth (scaled up toward the cap at runtime)
_HC_MAX_OFFSET = 10000  # Hardcover-popular is finite + overlaps heavily; don't page it past this
_RESOLVE_TTL = 6 * 3600  # don't re-hit the APIs for the same query within this window


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _recently_resolved(nk: str) -> bool:
    now = time.monotonic()
    exp = _resolve_seen.get(nk)
    return exp is not None and exp > now


def _mark_resolved(nk: str) -> None:
    now = time.monotonic()
    _resolve_seen[nk] = now + _RESOLVE_TTL
    if len(_resolve_seen) > _RESOLVE_SEEN_MAX:  # prune expired, then the soonest-to-expire
        for k in [k for k, e in _resolve_seen.items() if e <= now]:
            _resolve_seen.pop(k, None)
        # Evict by EARLIEST expiry, not dict-insertion order: updating an existing key doesn't reorder
        # it, so insertion order isn't expiry order and could reopen a still-fresh window early.
        while len(_resolve_seen) > _RESOLVE_SEEN_MAX:
            _resolve_seen.pop(min(_resolve_seen, key=_resolve_seen.get), None)


def _upsert_one(db: Session, hit: "BookHit") -> bool:
    """Upsert a single hit inside a SAVEPOINT so one bad row rolls back only itself, leaving the
    rest of the batch intact (the caller commits once at the end)."""
    try:
        with db.begin_nested():
            res = upsert_hit(db, hit)
        return res is not None
    except Exception:  # noqa: BLE001 — savepoint already rolled back this row
        return False


# --------------------------------------------------------------------- config/state
def get_config(db: Session) -> dict:
    row = db.get(AppSetting, _CONFIG_KEY)
    cfg = dict(_DEFAULTS)
    if row and isinstance(row.value, dict):
        cfg.update({k: row.value[k] for k in _DEFAULTS if k in row.value})
    return cfg


def set_config(db: Session, patch: dict) -> dict:
    cfg = get_config(db)
    for k in _DEFAULTS:
        if k in patch and patch[k] is not None:
            cfg[k] = patch[k]
    row = db.get(AppSetting, _CONFIG_KEY)
    if row is None:
        db.add(AppSetting(key=_CONFIG_KEY, value=cfg))
    else:
        row.value = cfg
    db.commit()
    return cfg


def _state(db: Session) -> dict:
    row = db.get(AppSetting, _STATE_KEY)
    return dict(row.value) if row and isinstance(row.value, dict) else {}


def _save_state(db: Session, state: dict) -> None:
    row = db.get(AppSetting, _STATE_KEY)
    if row is None:
        db.add(AppSetting(key=_STATE_KEY, value=state))
    else:
        row.value = state
    db.commit()


def book_row_count(db: Session) -> int:
    return int(db.scalar(
        select(func.count(CatalogWork.id)).where(CatalogWork.provider.in_(BOOK_PROVIDERS))
    ) or 0)


# --------------------------------------------------------------------- normalized hit
@dataclass
class BookHit:
    source: str                  # googlebooks | openlibrary
    ref: str
    title: str
    author: str | None = None
    year: int | None = None
    cover_url: str | None = None
    synopsis: str | None = None
    media_kind: str = "text"
    language: str | None = None
    popularity: float = 0.0      # raw audience signal (GB ratingsCount / OL readinglog_count)
    url: str | None = None
    isbn: list[str] = field(default_factory=list)
    subjects: list[str] = field(default_factory=list)
    series: str | None = None             # raw series label (e.g. "Mistborn (1)") when known
    series_position: float | None = None  # this book's position in the series, if known
    series_id: str | None = None          # stable canonical series id ("hc:<id>"), when the source has one
    # True when popularity is only a weak proxy (e.g. OL subject edition_count), so the row should
    # still be picked up by the enrichment tick for a real audience signal rather than frozen.
    weak_signal: bool = False


def _lang(code: str | None) -> str | None:
    if not code:
        return None
    code = code.strip().lower()
    return {"eng": "en", "english": "en"}.get(code, code[:2] or None)


# --------------------------------------------------------------------- Google Books
def _gb_key(db: Session) -> str:
    integ = db.scalar(select(Integration).where(Integration.kind == "googlebooks"))
    return (integ.api_key if integ else "") or ""


# --------------------------------------------------------------------- Hardcover
def _hc_token(db: Session) -> str:
    integ = db.scalar(
        select(Integration).where(Integration.kind == "hardcover", Integration.enabled.is_(True))
    )
    return ((integ.api_key if integ else "") or "").strip()


def _hc_doc_to_hit(doc: dict) -> BookHit | None:
    from ..integrations.metadata import _hc_authors, _hc_image
    title = (doc.get("title") or "").strip()
    ref = str(doc.get("id") or doc.get("slug") or "")
    if not title or not ref or _JUNK_TITLE_RE.match(title):
        return None
    pop = doc.get("users_count")
    series = (doc.get("series_names") or [None])
    slug = doc.get("slug")
    return BookHit(
        source="hardcover", ref=ref, title=title, author=_hc_authors(doc),
        year=doc.get("release_year"), cover_url=_hc_image(doc),
        synopsis=(doc.get("description") or "").strip() or None, media_kind="text",
        popularity=float(pop) if isinstance(pop, (int, float)) and pop > 0 else 0.0,
        url=f"https://hardcover.app/books/{slug}" if slug else f"hardcover:{ref}",
        isbn=[i for i in (doc.get("isbns") or []) if isinstance(i, str)][:5],
        series=series[0] if series else None,
    )


async def _hc_query(client: httpx.AsyncClient, *, q: str, limit: int, token: str) -> list[BookHit]:
    """Search Hardcover.app for `q`. Requires a Bearer token; returns [] without one."""
    if not token:
        return []
    from ..integrations.metadata import HARDCOVER_API, _HC_SEARCH_Q, _hc_hits, _hc_norm_token
    tok = _hc_norm_token(token)
    try:
        r = await client.post(
            HARDCOVER_API,
            json={"query": _HC_SEARCH_Q, "variables": {"q": q, "n": min(25, max(1, limit))}},
            headers={"Authorization": f"Bearer {tok}", "Accept": "application/json", "User-Agent": _UA},
        )
    except httpx.HTTPError as exc:
        log.info("hardcover query failed: %s", exc)
        return []
    if r.status_code != 200:
        log.info("hardcover HTTP %s: %s", r.status_code, r.text[:150])
        return []
    data = (r.json() or {}).get("data") or {}
    out: list[BookHit] = []
    for doc in _hc_hits(data):
        h = _hc_doc_to_hit(doc)
        if h:
            out.append(h)
    return out


def _gb_to_hit(it: dict) -> BookHit | None:
    from ..integrations.metadata import _gb_cover, _gb_media_kind, _gb_year
    vi = it.get("volumeInfo") or {}
    if not vi.get("title") or not it.get("id"):
        return None
    ratings = vi.get("ratingsCount")
    return BookHit(
        source="googlebooks",
        ref=str(it["id"]),
        title=vi.get("title") or "",
        author=", ".join(vi.get("authors") or []) or None,
        year=_gb_year(vi.get("publishedDate")),
        cover_url=_gb_cover(vi.get("imageLinks")) or _isbn_cover(
            [i.get("identifier") for i in (vi.get("industryIdentifiers") or [])]),
        synopsis=(vi.get("description") or "").strip() or None,
        media_kind=_gb_media_kind(vi.get("categories")),
        language=_lang(vi.get("language")),
        popularity=float(ratings) if isinstance(ratings, (int, float)) and ratings > 0 else 0.0,
        url=vi.get("infoLink") or vi.get("canonicalVolumeLink"),
        isbn=[i.get("identifier") for i in (vi.get("industryIdentifiers") or []) if i.get("identifier")],
        subjects=[str(c) for c in (vi.get("categories") or [])],
    )


# ISO-639-1 → the 3-letter code Open Library's `language` filter expects, for the languages we stock.
_OL_LANG3 = {"en": "eng", "no": "nor", "de": "ger", "fr": "fre", "es": "spa", "sv": "swe", "da": "dan"}


def _restrict_lang() -> str | None:
    """The single content language (2-letter) to restrict a metadata query to, or None when the
    instance stocks 0 or >1 languages — then we query ALL languages and let the catalog tag each
    result's language, rather than hiding e.g. Norwegian editions behind an English-only filter."""
    from .. import config_store
    langs = config_store.content_languages()
    return langs[0] if len(langs) == 1 else None


async def _gb_query(client: httpx.AsyncClient, *, q: str, limit: int, key: str,
                    start_index: int = 0) -> list[BookHit]:
    # Restrict to the instance's language only when exactly one is configured (default "en" → historical
    # English-canonical behavior; "no" → Norwegian). With several configured we DON'T restrict, so a
    # title's editions in every stocked language come through and the catalog tags each by language.
    params = {"q": q, "maxResults": min(40, limit), "printType": "books", "startIndex": start_index}
    if (code := _restrict_lang()):
        params["langRestrict"] = code
    if key:
        params["key"] = key
    try:
        r = await client.get(f"{GOOGLE_BOOKS_API}/volumes", params=params,
                             headers={"Accept": "application/json", "User-Agent": _UA})
    except httpx.HTTPError as exc:
        log.info("google books query failed: %s", exc)
        return []
    if r.status_code != 200:
        log.info("google books HTTP %s: %s", r.status_code, r.text[:150])
        return []
    out: list[BookHit] = []
    for it in (r.json() or {}).get("items", []) or []:
        h = _gb_to_hit(it)
        if h:
            out.append(h)
    return out


# --------------------------------------------------------------------- Open Library
_OL_SEARCH_FIELDS = (
    "key,title,author_name,first_publish_year,cover_i,readinglog_count,"
    "ratings_count,ratings_average,language,subject,isbn,series"
)


# ``?default=false`` is essential: without it Open Library's cover CDN returns a blank 1×1
# placeholder at HTTP 200 when a cover is missing — which the cover localizer would store as a
# PERMANENT blank /covers/ file (it only content-detects the Google Books placeholder). With it, a
# missing cover 404s, so cache_cover treats it as a permanent fail and the row falls back to a
# generated cover instead of a durable blank.
_OL_COVER_NODEFAULT = "?default=false"


def _ol_cover(cover_i) -> str | None:
    if not cover_i:
        return None
    return f"https://covers.openlibrary.org/b/id/{cover_i}-M.jpg{_OL_COVER_NODEFAULT}"


def _isbn_cover(isbns: list | None) -> str | None:
    """Cross-source cover fallback: Open Library serves covers by ISBN (keyless CDN) for a huge
    range of books, so a row that has an ISBN but no provider cover still gets art."""
    for raw in isbns or []:
        digits = re.sub(r"[^0-9Xx]", "", str(raw))
        if len(digits) in (10, 13):
            return f"https://covers.openlibrary.org/b/isbn/{digits}-M.jpg{_OL_COVER_NODEFAULT}"
    return None


def _ol_doc_to_hit(b: dict) -> BookHit | None:
    title = (b.get("title") or "").strip()
    key = b.get("key") or ""
    if not title or not key or _JUNK_TITLE_RE.match(title):
        return None
    pop = b.get("readinglog_count")
    langs = b.get("language") or []
    # OL search returns every edition's language; prefer English when present so the catalog
    # language reflects the readable edition (matters for matching), else fall back to the first.
    lang = "en" if any(l in ("eng", "en") for l in langs) else _lang(langs[0] if langs else None)
    return BookHit(
        source="openlibrary",
        ref=key,                          # e.g. /works/OL45883W
        title=title,
        author=", ".join(b.get("author_name") or []) or None,
        year=b.get("first_publish_year"),
        cover_url=_ol_cover(b.get("cover_i")) or _isbn_cover(b.get("isbn")),
        media_kind="text",
        language=lang,
        popularity=float(pop) if isinstance(pop, (int, float)) and pop > 0 else 0.0,
        url=f"{OPENLIBRARY}{key}",
        isbn=(b.get("isbn") or [])[:5],
        subjects=[str(s) for s in (b.get("subject") or [])][:12],
        series=(b.get("series") or [None])[0] if b.get("series") else None,
    )


async def _ol_search(client: httpx.AsyncClient, *, title: str, author: str | None,
                     limit: int) -> list[BookHit]:
    # Base URL is the fixed OPENLIBRARY host; the user-influenced title/author go through httpx's
    # `params=` (separately encoded) so they can never alter the host/path — no SSRF surface.
    # Restrict to the one configured content language (mapped to OL's 3-letter code), else query all
    # languages — same rule as _gb_query, so Norwegian editions aren't hidden behind an English filter.
    params = {"title": title, "limit": limit, "fields": _OL_SEARCH_FIELDS}
    if (code := _restrict_lang()) and (lang3 := _OL_LANG3.get(code)):
        params["language"] = lang3
    if author:
        params["author"] = author
    try:
        r = await client.get(f"{OPENLIBRARY}/search.json", params=params,
                              headers={"Accept": "application/json", "User-Agent": _UA})
    except httpx.HTTPError as exc:
        log.info("open library search failed: %s", exc)
        return []
    if r.status_code != 200:
        log.info("open library HTTP %s", r.status_code)
        return []
    out: list[BookHit] = []
    for b in (r.json() or {}).get("docs", []) or []:
        h = _ol_doc_to_hit(b)
        if h:
            out.append(h)
    return out


async def _ol_trending(client: httpx.AsyncClient, period: str, *, limit: int = 100) -> list[BookHit]:
    url = f"{OPENLIBRARY}/trending/{period}.json?limit={limit}"
    try:
        r = await client.get(url, headers={"Accept": "application/json", "User-Agent": _UA})
    except httpx.HTTPError as exc:
        log.info("open library trending failed: %s", exc)
        return []
    if r.status_code != 200:
        return []
    out: list[BookHit] = []
    for w in (r.json() or {}).get("works", []) or []:
        h = _ol_doc_to_hit(w)
        if h:
            out.append(h)
    return out


async def _ol_subject(client: httpx.AsyncClient, subject: str, offset: int,
                      *, limit: int = _PAGE) -> list[BookHit]:
    url = f"{OPENLIBRARY}/subjects/{subject}.json?limit={limit}&offset={offset}"
    try:
        r = await client.get(url, headers={"Accept": "application/json", "User-Agent": _UA})
    except httpx.HTTPError as exc:
        log.info("open library subject failed: %s", exc)
        return []
    if r.status_code != 200:
        return []
    out: list[BookHit] = []
    for w in (r.json() or {}).get("works", []) or []:
        key = w.get("key") or ""
        title = (w.get("title") or "").strip()
        if not key or not title:
            continue
        authors = [a.get("name") for a in (w.get("authors") or []) if a.get("name")]
        if _JUNK_TITLE_RE.match(title):
            continue
        out.append(BookHit(
            source="openlibrary", ref=key, title=title,
            author=", ".join(authors) or None,
            year=w.get("first_publish_year"),
            cover_url=_ol_cover(w.get("cover_id") or w.get("cover_i")),
            media_kind="text",
            popularity=float(w.get("edition_count") or 0),  # weak proxy until search enriches it
            url=f"{OPENLIBRARY}{key}",
            subjects=[subject.replace("_", " ")],
            weak_signal=True,
        ))
    return out


# --------------------------------------------------------------------- upsert
def upsert_hit(db: Session, hit: BookHit) -> CatalogWork | None:
    """Create/update a book CatalogWork row from a normalized hit. Deduped by
    (provider, provider_ref). Respects the operator blocklist. Does NOT commit."""
    from . import blocklist
    from .catalog_enrichment import _ol_genres, _tags

    if hit.url and blocklist.is_blocked(db, hit.url):
        return None
    entry = db.scalar(
        select(CatalogWork).where(
            CatalogWork.provider == hit.source,
            CatalogWork.provider_ref == hit.ref,
            CatalogWork.integration_id.is_(None),
        )
    )
    if entry is None:
        entry = CatalogWork(
            provider=hit.source, provider_ref=hit.ref, integration_id=None,
            domain=_DOMAIN.get(hit.source, hit.source),
            work_url=hit.url or f"{hit.source}:{hit.ref}", title=hit.title[:512],
        )
        db.add(entry)
    entry.title = hit.title[:512]
    entry.norm_key = norm_title(hit.title)
    if hit.author:
        entry.author = hit.author[:255]
    # Adopt a cover when we don't have one — or when the one we have is a broken legacy imgcache path
    # (LRU-evicted, unfetchable), so a re-ingest heals it instead of preserving a dead cover forever.
    if hit.cover_url and (not entry.cover_url or "/imgcache/" in entry.cover_url):
        entry.cover_url = hit.cover_url
    if hit.synopsis and (not entry.synopsis or len(hit.synopsis) > len(entry.synopsis or "")):
        entry.synopsis = hit.synopsis
    entry.media_kind = hit.media_kind
    entry.kind = "work"
    if hit.language:
        entry.language = hit.language
    if hit.year:
        entry.year = hit.year
    if hit.url:
        entry.work_url = hit.url
    if hit.popularity and hit.popularity > 0:
        entry.popularity = float(hit.popularity)
    genres = _tags(_ol_genres(hit.subjects)) if hit.subjects else []
    extra = dict(entry.extra or {})
    extra["source"] = hit.source
    if hit.isbn:
        extra["isbn"] = hit.isbn
        # Stamp the deterministic cross-source merge key from the first usable ISBN (MERGE-2).
        # First-id-wins (only when unset) so a re-ingest doesn't churn it. The normalized ISBN-13
        # lets two rows of the same book from different providers merge in the regroup pass.
        if not entry.identity_key:
            from .verify import _norm_isbn
            for raw in hit.isbn:
                norm = _norm_isbn(raw)
                if norm:
                    entry.identity_key = f"isbn:{norm}"[:64]
                    break
    if hit.series:
        extra["series"] = hit.series
    if hit.series_position is not None:
        extra["series_position"] = hit.series_position
    if hit.series_id:
        extra["series_id"] = hit.series_id
    if genres:
        extra["genres"] = genres
        # We already have genres AND a real audience signal — spare the enrich tick a redundant
        # lookup. Weak-signal rows (subject edition_count proxy) stay un-stamped so the tick can
        # later upgrade their popularity to a real readinglog count.
        if entry.enriched_at is None and not hit.weak_signal:
            entry.enriched_at = _utcnow()
            entry.enrich_source = hit.source
    entry.extra = extra
    # Compute the 18+ flag HERE too. When a book hit carries genres + a real signal we stamp
    # enriched_at above, which makes the enrichment tick (the other place is_adult is set) skip this
    # row forever — so an adult-genre book (e.g. "Erotica" subject) would otherwise stay
    # is_adult=False and leak into non-18+ browse. Re-deriving it from the taxonomy we just wrote
    # closes that gap and is idempotent for the un-stamped/weak-signal path the tick still handles.
    from . import catalog as _catalog
    entry.is_adult = _catalog.taxonomy_is_adult(extra)
    entry.updated_at = _utcnow()
    return entry


# --------------------------------------------------------------------- closeness gate
def closeness(query: str, rows: list[CatalogWork]) -> float:
    """How closely the best local row matches the query (0..1), by normalized-title token
    Jaccard with a boost when the query is fully contained in a title."""
    nq = set(norm_title(query).split())
    if not nq:
        return 0.0
    best = 0.0
    for r in rows:
        nk = set((r.norm_key or norm_title(r.title)).split())
        if not nk:
            continue
        jac = len(nq & nk) / len(nq | nk)
        if nq <= nk:  # query fully contained in the title
            jac = max(jac, 0.85)
        best = max(best, jac)
        if best >= 0.999:
            break
    return best


async def _search_all(db: Session, query: str, *, limit: int) -> list[BookHit]:
    key = _gb_key(db)
    hc_token = _hc_token(db)
    async with telemetry.instrument("metadata", timeout=_TIMEOUT, follow_redirects=True) as client:
        gb, ol, hc = await asyncio.gather(
            _gb_query(client, q=query, limit=limit, key=key),
            _ol_search(client, title=query, author=None, limit=limit),
            _hc_query(client, q=query, limit=limit, token=hc_token),
            return_exceptions=True,
        )
    hits: list[BookHit] = []
    for res in (gb, ol, hc):
        if isinstance(res, list):
            hits.extend(res)
        elif isinstance(res, Exception):
            log.info("book search source failed: %s", res)
    return hits


async def resolve_live(db: Session, query: str, *, limit: int = 10) -> int:
    """Query Google Books + Open Library for `query`, upsert the results, return how many rows
    were added/updated. Guarded per normalized query so repeats/pagination don't re-hit the APIs."""
    nk = norm_title(query)
    if not nk or _recently_resolved(nk):
        return 0
    _mark_resolved(nk)
    hits = await _search_all(db, query, limit=limit)
    n = sum(1 for h in hits if _upsert_one(db, h))
    if n:
        db.commit()
    return n


async def resolve_if_sparse(db: Session, query: str) -> bool:
    """The closeness gate: if the local catalog has no sufficiently-close match for `query`,
    resolve it live. Returns True if a live resolve ran (so the caller invalidates caches)."""
    from . import catalog
    cfg = get_config(db)
    if not cfg["enabled"]:
        return False
    # Already resolved recently → don't even probe the catalog (skip the extra find_rows).
    if _recently_resolved(norm_title(query)):
        return False
    local = catalog.find_rows(db, q=query, limit=40)
    if local and closeness(query, local) >= float(cfg["closeness_threshold"]):
        return False
    added = await resolve_live(db, query)
    return added > 0


# --------------------------------------------------------------------- Hardcover popular seed
_HC_PAGE = 50            # books per popular-seed request
_HC_POPULAR_Q = (
    "query($lim:Int!,$off:Int!){ books(order_by:{users_count:desc}, limit:$lim, offset:$off, "
    "where:{users_count:{_gt:0}}){ id slug title release_year users_count rating description "
    "image{url} cached_image contributions{author{name}} cached_tags "
    "book_series{ position series{ id name books_count } } } }"
)


def _hc_series_info(b: dict) -> tuple[str | None, float | None, str | None]:
    """The multi-volume series this book belongs to (≥2 books), this book's position, and the stable
    canonical series id ("hc:<id>") if Hardcover provides one — stored so the UI shows 'View Series'
    only for real series, the library can order volumes, and dedup can key on the id (Project 2)."""
    for bs in b.get("book_series") or []:
        s = (bs.get("series") or {}) if isinstance(bs, dict) else {}
        name = s.get("name")
        if name and int(s.get("books_count") or 0) >= 2:
            pos = bs.get("position")
            sid = s.get("id")
            return (name, (float(pos) if isinstance(pos, (int, float)) else None),
                    f"hc:{sid}" if sid is not None else None)
    return None, None, None


def _hc_series_name(b: dict) -> str | None:
    return _hc_series_info(b)[0]


def _hc_genres(cached) -> list[str]:
    """Genre tag names from a Hardcover book's cached_tags ({'Genre':[{'tag':'Fantasy',...}]})."""
    if not isinstance(cached, dict):
        return []
    return [t.get("tag") for t in (cached.get("Genre") or [])
            if isinstance(t, dict) and t.get("tag")][:12]


def _hc_book_to_hit(b: dict) -> BookHit | None:
    title = (b.get("title") or "").strip()
    bid = b.get("id")
    if not title or bid is None or _JUNK_TITLE_RE.match(title):
        return None
    authors = [c["author"]["name"] for c in (b.get("contributions") or [])
               if isinstance(c, dict) and (c.get("author") or {}).get("name")]
    uc = b.get("users_count")
    img = (b.get("image") or {}).get("url") if isinstance(b.get("image"), dict) else None
    if not img and isinstance(b.get("cached_image"), dict):
        img = b["cached_image"].get("url")     # some books have a null image relation but a cache
    slug = b.get("slug")
    return BookHit(
        source="hardcover", ref=str(bid), title=title,
        author=", ".join(dict.fromkeys(authors)) or None,
        year=b.get("release_year"), cover_url=img,
        synopsis=(b.get("description") or "").strip() or None, media_kind="text",
        popularity=float(uc) if isinstance(uc, (int, float)) and uc > 0 else 0.0,
        url=f"https://hardcover.app/books/{slug}" if slug else f"hardcover:{bid}",
        subjects=_hc_genres(b.get("cached_tags")), weak_signal=False,
        series=(_si := _hc_series_info(b))[0], series_position=_si[1], series_id=_si[2],
    )


async def _hc_popular(client: httpx.AsyncClient, token: str, *, offset: int,
                      limit: int = _HC_PAGE) -> list[BookHit]:
    """The most-popular books on Hardcover (by users_count), paginated — authoritative popularity
    ranking + covers + genres for the hot set."""
    from ..integrations.metadata import HARDCOVER_API, _hc_norm_token
    tok = _hc_norm_token(token)
    if not tok:
        return []
    try:
        r = await client.post(
            HARDCOVER_API, json={"query": _HC_POPULAR_Q, "variables": {"lim": limit, "off": offset}},
            headers={"Authorization": f"Bearer {tok}", "Accept": "application/json", "User-Agent": _UA},
        )
    except httpx.HTTPError as exc:
        log.info("hardcover popular failed: %s", exc)
        return []
    if r.status_code != 200:
        log.info("hardcover popular HTTP %s: %s", r.status_code, r.text[:150])
        return []
    data = (r.json() or {}).get("data") or {}
    out: list[BookHit] = []
    for b in data.get("books") or []:
        h = _hc_book_to_hit(b)
        if h:
            out.append(h)
    return out


# --------------------------------------------------------------------- hot-set seeding
def _initial_cursor(db: Session) -> dict:
    """Start the seed with Hardcover's most-popular books (real popularity + covers) when a token is
    configured; otherwise begin with Open Library trending."""
    if _hc_token(db):
        return {"phase": "hc_popular", "offset": 0}
    return {"phase": "trending", "i": 0, "offset": 0}


async def sync_hot_set(db: Session, *, max_requests: int = 4) -> dict:
    """Advance the resumable hot-set seed by a bounded number of API requests. Trending is always
    refreshed; subject pages stop once the book-row count reaches the configured cap. Re-seeds from
    scratch once a full pass is a week old."""
    cfg = get_config(db)
    if not cfg["enabled"]:
        return {"enabled": False}
    # Non-blocking: if a seed run (tick or manual) is already in flight, skip rather than race the
    # cursor read-modify-write (which would discard one run's pagination progress).
    if not _sync_lock.acquire(blocking=False):
        return {"skipped": "already running"}
    try:
        return await _sync_hot_set_locked(db, cfg, max_requests=max_requests)
    finally:
        _sync_lock.release()


async def _sync_hot_set_locked(db: Session, cfg: dict, *, max_requests: int) -> dict:
    state = _state(db)
    cursor = state.get("cursor") or _initial_cursor(db)
    cap = int(cfg["hot_set_cap"])
    count = book_row_count(db)
    # Paginate each subject deep enough that the configured cap is actually reachable from subjects
    # (not just the old fixed 200/subject = ~3.2k ceiling that left a 100k cap stuck near 20k). Bound
    # it so a tiny cap still pages a sensible minimum.
    subject_depth = max(_SUBJECT_DEPTH, -(-cap // max(1, len(_SUBJECTS))))

    # Restart a completed pass when (a) the cap was RAISED since the last full pass and there's room
    # to grow — so a bigger cap actually seeds more instead of staying parked at the old size — or
    # (b) it's been a week (keep trending/popularity fresh). Without (a), raising hot_set_cap did
    # nothing: the pass was already 'done' and only re-ran weekly.
    if cursor.get("phase") == "done":
        last = state.get("last_full_at")
        stale = True
        if last:
            try:
                stale = datetime.fromisoformat(last) < _utcnow() - timedelta(days=7)
            except ValueError:
                stale = True
        cap_raised = cap > int(state.get("seeded_cap", 0)) and count < cap
        if not (stale or cap_raised):
            return {"phase": "done", "added": 0}
        # Weekly refresh re-runs everything (keep trending/popularity fresh); a cap raise jumps
        # straight to subjects — hc_popular/trending are already saturated, so re-running them would
        # just churn duplicates without growing toward the larger cap.
        cursor = _initial_cursor(db) if stale else {"phase": "subjects", "i": 0, "offset": 0}
    added = reqs = 0
    async with telemetry.instrument("metadata", timeout=_TIMEOUT, follow_redirects=True) as client:
        while reqs < max_requests and cursor.get("phase") != "done":
            phase = cursor["phase"]
            if phase == "hc_popular":
                # Seed the most-popular books from Hardcover first (real popularity + covers). Bounded
                # by _HC_MAX_OFFSET, not the (possibly huge) cap: Hardcover's popular list is finite
                # and heavily overlaps already-seeded rows, so paging it to a 100k offset just churns
                # duplicates — the bulk growth comes from the subjects phase below.
                token = _hc_token(db)
                if not token or book_row_count(db) >= cap or cursor["offset"] >= _HC_MAX_OFFSET:
                    cursor = {"phase": "trending", "i": 0, "offset": 0}
                    continue
                hits = await _hc_popular(client, token, offset=cursor["offset"])
                reqs += 1
                added += _absorb(db, hits)
                cursor["offset"] += _HC_PAGE
                if not hits:
                    cursor = {"phase": "trending", "i": 0, "offset": 0}
            elif phase == "trending":
                hits = await _ol_trending(client, _TRENDING[cursor["i"]])
                reqs += 1
                added += _absorb(db, hits)
                cursor["i"] += 1
                if cursor["i"] >= len(_TRENDING):
                    cursor = {"phase": "subjects", "i": 0, "offset": 0}
            elif phase == "subjects":
                # Use the LIVE row count, not count+added: _absorb counts upserts (incl. duplicates),
                # so count+added overstated progress and could mark 'done' without real growth.
                if book_row_count(db) >= cap:
                    cursor = {"phase": "done"}
                    break
                subj = _SUBJECTS[cursor["i"]]
                ol_hits = await _ol_subject(client, subj, cursor["offset"])
                reqs += 1
                added += _absorb(db, ol_hits)
                if reqs < max_requests:
                    gb_hits = await _gb_query(
                        client, q=f"subject:{subj.replace('_', ' ')}",
                        limit=40, key=_gb_key(db), start_index=cursor["offset"],
                    )
                    reqs += 1
                    added += _absorb(db, gb_hits)
                cursor["offset"] += _PAGE
                if cursor["offset"] >= subject_depth or not ol_hits:
                    cursor["i"] += 1
                    cursor["offset"] = 0
                    if cursor["i"] >= len(_SUBJECTS):
                        cursor = {"phase": "done"}
            db.commit()

    state["cursor"] = cursor
    if cursor.get("phase") == "done":
        state["last_full_at"] = _utcnow().isoformat()
        state["seeded_cap"] = cap   # remember the cap this pass seeded for → re-seed when it's raised
    state["count"] = book_row_count(db)
    _save_state(db, state)
    return {"added": added, "requests": reqs, "phase": cursor.get("phase"), "count": state["count"]}


def _absorb(db: Session, hits: list[BookHit]) -> int:
    return sum(1 for h in hits if _upsert_one(db, h))


# --------------------------------------------------------------------- detail fetches (synopsis)
def _clean_ol_description(desc) -> str | None:
    """Open Library descriptions are a string or a {type, value} dict, and carry markup noise: a
    'source' footer after a horizontal rule, stray HTML, and markdown links (some records are just a
    bare "[link](url)" to a PDF — not a real synopsis). Return clean prose, or None when what's left
    is too thin to be a real description."""
    if isinstance(desc, dict):
        desc = desc.get("value")
    if not isinstance(desc, str):
        return None
    desc = re.split(r"\r?\n-{3,}", desc, maxsplit=1)[0]               # drop a "----" source footer
    desc = re.sub(r"\(\[source\]\[\d+\]\).*$", "", desc, flags=re.S)  # or a "([source][1])" one
    desc = re.sub(r"\[([^\]]+)\]\((?:https?:)?[^)]+\)", r"\1", desc)  # [text](url) → text
    desc = re.sub(r"<[^>]+>", "", desc)                              # stray HTML (<u>…</u>)
    desc = re.sub(r"https?://\S+", "", desc)                         # bare URLs
    desc = re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]{2,}", " ", desc)).strip()
    # A real synopsis is prose, not a 3-word link label left over after stripping — drop the scraps.
    return desc if len(desc) >= 40 else None


async def _ol_work_detail(client: httpx.AsyncClient, key: str | None) -> tuple[str | None, str | None]:
    """``(synopsis, cover_url)`` from an Open Library WORK detail endpoint. The bulk search/subject
    APIs we seed from omit the description AND often the cover id (the ``covers`` list lives on the
    work record), so this is where OL-sourced rows get both. ``key`` is a work key like
    ``/works/OL45883W``."""
    if not key or not key.startswith("/works/"):
        return None, None
    try:
        r = await client.get(f"{OPENLIBRARY}{key}.json",
                             headers={"Accept": "application/json", "User-Agent": _UA})
    except httpx.HTTPError as exc:
        log.info("open library work fetch failed: %s", exc)
        return None, None
    if r.status_code != 200:
        return None, None
    j = r.json() or {}
    cover_id = next((c for c in (j.get("covers") or []) if isinstance(c, int) and c > 0), None)
    return _clean_ol_description(j.get("description")), _ol_cover(cover_id)


async def _gb_volume_detail(client: httpx.AsyncClient, ref: str, key: str) -> tuple[str | None, str | None]:
    """``(synopsis, cover_url)`` from a Google Books volume by id (search usually carries them, but
    some hits arrive without the full volumeInfo)."""
    if not ref:
        return None, None
    from ..integrations.metadata import _gb_cover
    params = {"key": key} if key else {}
    try:
        r = await client.get(f"{GOOGLE_BOOKS_API}/volumes/{ref}", params=params,
                             headers={"Accept": "application/json", "User-Agent": _UA})
    except httpx.HTTPError as exc:
        log.info("google books volume fetch failed: %s", exc)
        return None, None
    if r.status_code != 200:
        return None, None
    vi = (r.json() or {}).get("volumeInfo") or {}
    return (vi.get("description") or "").strip() or None, _gb_cover(vi.get("imageLinks"))


async def _provider_detail(client: httpx.AsyncClient, row: CatalogWork,
                           gb_key: str) -> tuple[str | None, str | None]:
    """``(synopsis, cover_url)`` from the row's OWN provider's detail endpoint."""
    ref = row.provider_ref or ""
    if row.provider == "openlibrary":
        return await _ol_work_detail(client, ref or None)
    if row.provider == "googlebooks":
        return await _gb_volume_detail(client, ref, gb_key)
    return None, None


# --------------------------------------------------------------------- long-tail backfill
async def _backfill_row(client: httpx.AsyncClient, row: CatalogWork, token: str, gb_key: str) -> bool:
    """Fill a row's missing COVER, SERIES, and SYNOPSIS from other providers: a Hardcover match
    (cover + series + synopsis), the row's OWN provider detail endpoint (the bulk search APIs omit
    the description, and OL omits the cover id too), and the Open Library ISBN-cover CDN as a last
    cover fallback. Cover/series and synopsis retire independently (``meta_checked`` / ``syn_checked``)
    so a row already cover-checked still gets a synopsis pass — and since the detail fetch surfaces a
    cover the earlier check lacked, that pass back-fills the cover too. Returns True if anything
    changed."""
    changed = False
    nk = row.norm_key or norm_title(row.title or "")
    extra = dict(row.extra or {})
    broken_cover = bool(row.cover_url and "/imgcache/" in row.cover_url)
    need_cover_series = extra.get("meta_checked") is None or broken_cover
    need_synopsis = not (row.synopsis or "").strip() and extra.get("syn_checked") is None
    if not (need_cover_series or need_synopsis):
        return False

    if broken_cover:
        # Salvage the file if it somehow survived the sweep; otherwise drop it so the re-source paths
        # below give it a fresh, durable cover.
        from .. import imagecache
        row.cover_url = imagecache.migrate_imgcache_cover(row.cover_url)  # /covers/… or None
        changed = True

    # Two lookups, each serving cover + series + synopsis: a Hardcover cross-match, and the row's own
    # provider detail endpoint. Both are gated by the one-time markers, so a barren row isn't re-hit.
    best = None
    if token:
        try:
            hits = await _hc_query(client, q=f"{row.title} {row.author or ''}".strip(),
                                   limit=5, token=token)
        except Exception:  # noqa: BLE001
            hits = []
        best = next((h for h in hits
                     if norm_title(h.title) == nk and authors_compatible(row.author, h.author)), None)
    det_syn, det_cover = await _provider_detail(client, row, gb_key)

    # Cover: any source heals it, even on an already-meta-checked row — the detail endpoint is a new
    # source the earlier cover check didn't have (e.g. OL's `covers` list, absent from search).
    if not row.cover_url:
        new_cover = (best.cover_url if best else None) or det_cover or _isbn_cover(extra.get("isbn"))
        if new_cover:
            row.cover_url = new_cover
            changed = True

    if need_cover_series:
        if best and not extra.get("series") and best.series:
            extra["series"] = best.series
            if best.series_id:
                extra["series_id"] = best.series_id
            changed = True
        extra["meta_checked"] = _utcnow().isoformat()

    if need_synopsis:
        syn = (best.synopsis if best else None) or det_syn
        if syn:
            row.synopsis = syn
            changed = True
        extra["syn_checked"] = _utcnow().isoformat()

    row.extra = extra
    return changed


async def backfill_metadata(db: Session, *, max_lookups: int = 20) -> dict:
    """Background long-tail backfill: find the most-popular book rows still missing a cover, a
    series tag, or a synopsis (and not yet checked for that field) and fill them from the metadata
    providers. Bounded per run and resumable (each field retires independently via meta_checked /
    syn_checked), so it converges without re-querying."""
    cfg = get_config(db)
    if not cfg["enabled"]:
        return {"enabled": False}
    if not _backfill_lock.acquire(blocking=False):
        return {"skipped": "already running"}
    try:
        rows = db.scalars(
            select(CatalogWork).where(
                CatalogWork.provider.in_(BOOK_PROVIDERS),
                or_(
                    # A cover localized into the LRU-swept imgcache is broken once evicted and can't be
                    # re-fetched (the remote URL was overwritten) — re-source it durably, even if the
                    # row was already meta-checked (its cover rotted AFTER that check).
                    CatalogWork.cover_url.like("%/imgcache/%"),
                    and_(
                        func.json_extract(CatalogWork.extra, "$.meta_checked").is_(None),
                        or_(
                            CatalogWork.cover_url.is_(None),
                            func.json_extract(CatalogWork.extra, "$.series").is_(None),
                        ),
                    ),
                    # Synopsis retires separately: the bulk search/subject APIs we seed from omit
                    # descriptions, so most rows arrive synopsis-less even after a cover check — this
                    # clause re-picks them (regardless of meta_checked) for a one-time detail fetch.
                    and_(
                        func.json_extract(CatalogWork.extra, "$.syn_checked").is_(None),
                        CatalogWork.synopsis.is_(None),
                    ),
                ),
            ).order_by(CatalogWork.popularity.desc()).limit(max_lookups)
        ).all()
        if not rows:
            return {"checked": 0, "updated": 0}
        token = _hc_token(db)
        gb_key = _gb_key(db)
        updated = 0
        async with telemetry.instrument("metadata", timeout=_TIMEOUT, follow_redirects=True) as client:
            for row in rows:
                try:
                    if await _backfill_row(client, row, token, gb_key):
                        updated += 1
                except Exception:  # noqa: BLE001 — never let one row abort the batch
                    log.info("backfill failed for %r", row.title)
                db.commit()
        wseries = _backfill_work_series(db)
        return {"checked": len(rows), "updated": updated, "work_series": wseries}
    finally:
        _backfill_lock.release()


def _backfill_work_series(db: Session, *, limit: int = 200) -> int:
    """Stamp series name/position onto existing library Works (imported before series capture) by
    matching a catalog row of the same title — so the library can group them. DB-only, cheap."""
    from ..models import Work
    works = db.scalars(select(Work).where(Work.series.is_(None)).limit(limit)).all()
    n = 0
    for w in works:
        cw = db.scalar(select(CatalogWork).where(
            CatalogWork.norm_key == norm_title(w.title or ""),
            func.json_extract(CatalogWork.extra, "$.series").is_not(None),
        ).limit(1))
        s = (cw.extra or {}).get("series") if (cw and isinstance(cw.extra, dict)) else None
        if s:
            w.series = str(s)[:255]
            p = (cw.extra or {}).get("series_position")
            if isinstance(p, (int, float)):
                w.series_position = float(p)
            sid = (cw.extra or {}).get("series_id")
            if sid:
                w.series_id = str(sid)[:64]
            n += 1
    if n:
        db.commit()
    return n


def status(db: Session) -> dict:
    st = _state(db)
    return {
        "config": get_config(db),
        "book_rows": book_row_count(db),
        "phase": (st.get("cursor") or {}).get("phase", "idle"),
        "last_full_at": st.get("last_full_at"),
    }


# --------------------------------------------------------- catalog local imports
def _best_local_catalog_match(db: Session, nk: str, author: str | None,
                              media_kind: str | None) -> CatalogWork | None:
    """The best not-yet-hooked book-provider catalog row for a local work: same normalized title +
    media class, author-compatible when the work has one."""
    bucket = "comic" if (media_kind or "text") == "comic" else "text"
    rows = db.scalars(select(CatalogWork).where(
        CatalogWork.norm_key == nk, CatalogWork.hooked_work_id.is_(None),
        CatalogWork.provider.in_(BOOK_PROVIDERS))).all()
    same = [r for r in rows
            if ("comic" if (r.media_kind or "text") == "comic" else "text") == bucket]
    if not same:
        return None
    if author:
        for r in same:
            if authors_compatible(author, r.author):
                return r
        return None  # a same-title DIFFERENT-author edition is the wrong book — don't mislink
    return same[0]


async def resolve_local_to_catalog(db: Session, work: Work) -> bool:
    """Surface a locally-imported book (watched folder / stock / orphaned download) in the catalog.

    A local file becomes a library ``Work`` but has no catalog entry, so it never shows in discovery
    and carries only the metadata embedded in the file. This matches it against the book metadata
    providers (Google Books / Open Library), links the best result as the work's catalog entry (and
    backfills cover/author/synopsis), or — failing a provider match — creates a minimal ``local``
    catalog row so it's at least surfaced. Returns True if newly catalogued."""
    if not (work.local_path and (work.title or "").strip()):
        return False
    nk = norm_title(work.title)
    if not nk:
        return False
    if db.scalar(select(CatalogWork.id).where(CatalogWork.hooked_work_id == work.id).limit(1)):
        return False  # already catalogued
    try:
        await resolve_live(db, f"{work.title} {work.author or ''}".strip())
    except Exception:  # noqa: BLE001 — provider down → fall back to a local entry
        log.info("resolve_live failed for local work %s", work.id, exc_info=True)
    cand = _best_local_catalog_match(db, nk, work.author, work.media_kind)
    if cand is None:
        cand = CatalogWork(provider="local", domain="local", work_url=f"local:{work.id}",
                           norm_key=nk, title=(work.title or "")[:512], author=work.author,
                           media_kind=work.media_kind or "text")
        db.add(cand)
        db.flush()
    cand.hooked_work_id = work.id
    if not work.cover_url and cand.cover_url:
        work.cover_url = cand.cover_url
    if not work.author and cand.author:
        work.author = cand.author
    if not work.description and cand.synopsis:
        work.description = cand.synopsis
    db.commit()
    log.info("catalogued local work %s -> %s:%s", work.id, cand.provider, cand.id)
    return True


async def catalog_local_tick(db: Session) -> dict:
    """Periodic: catalog a bounded batch of local library Works that have no catalog entry yet, so
    files imported from a watched/stock folder (or an orphaned download) get matched to metadata and
    surfaced in the catalog. Polite to the providers (small batch + spacing)."""
    hooked = select(CatalogWork.hooked_work_id).where(CatalogWork.hooked_work_id.is_not(None))
    works = db.scalars(
        select(Work).where(Work.local_path.is_not(None), Work.id.not_in(hooked))
        .order_by(Work.id).limit(15)
    ).all()
    catalogued = 0
    for w in works:
        try:
            if await resolve_local_to_catalog(db, w):
                catalogued += 1
        except Exception:  # noqa: BLE001 — one work failing must not abort the batch
            db.rollback()
            log.exception("catalog_local_tick failed for work %s", w.id)
        await asyncio.sleep(0.3)
    return {"scanned": len(works), "catalogued": catalogued}
