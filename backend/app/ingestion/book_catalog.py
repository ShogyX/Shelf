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
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import AppSetting, CatalogWork, Integration
from .extract import norm_title

log = logging.getLogger("shelf.book_catalog")

# Serialize hot-set seeding across the scheduled tick and the manual /catalog/book-sync endpoint
# (both run in this process) so two runs can't read-modify-write the cursor and lose progress.
_sync_lock = threading.Lock()

# Per-query live-resolve guard: a normalized query is resolved against the APIs at most once per
# window. Kept in a dedicated time-pruned dict (NOT the shared LRU read-cache, whose 512-entry
# eviction would re-open the window early under varied search load).
_resolve_seen: dict[str, float] = {}
_RESOLVE_SEEN_MAX = 5000

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
_SUBJECT_DEPTH = 200  # how deep to paginate each subject before moving on
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
    if len(_resolve_seen) > _RESOLVE_SEEN_MAX:  # prune expired, then oldest
        for k in [k for k, e in _resolve_seen.items() if e <= now]:
            _resolve_seen.pop(k, None)
        while len(_resolve_seen) > _RESOLVE_SEEN_MAX:
            _resolve_seen.pop(next(iter(_resolve_seen)), None)


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
        cover_url=_gb_cover(vi.get("imageLinks")),
        synopsis=(vi.get("description") or "").strip() or None,
        media_kind=_gb_media_kind(vi.get("categories")),
        language=_lang(vi.get("language")),
        popularity=float(ratings) if isinstance(ratings, (int, float)) and ratings > 0 else 0.0,
        url=vi.get("infoLink") or vi.get("canonicalVolumeLink"),
        isbn=[i.get("identifier") for i in (vi.get("industryIdentifiers") or []) if i.get("identifier")],
        subjects=[str(c) for c in (vi.get("categories") or [])],
    )


async def _gb_query(client: httpx.AsyncClient, *, q: str, limit: int, key: str,
                    start_index: int = 0) -> list[BookHit]:
    params = {"q": q, "maxResults": min(40, limit), "printType": "books", "startIndex": start_index}
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


def _ol_cover(cover_i) -> str | None:
    return f"https://covers.openlibrary.org/b/id/{cover_i}-M.jpg" if cover_i else None


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
        cover_url=_ol_cover(b.get("cover_i")),
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
    from urllib.parse import quote_plus
    q = f"title={quote_plus(title)}" + (f"&author={quote_plus(author)}" if author else "")
    url = f"{OPENLIBRARY}/search.json?{q}&limit={limit}&fields={_OL_SEARCH_FIELDS}"
    try:
        r = await client.get(url, headers={"Accept": "application/json", "User-Agent": _UA})
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
    if hit.cover_url and not entry.cover_url:
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
    if hit.series:
        extra["series"] = hit.series
    if genres:
        extra["genres"] = genres
        # We already have genres AND a real audience signal — spare the enrich tick a redundant
        # lookup. Weak-signal rows (subject edition_count proxy) stay un-stamped so the tick can
        # later upgrade their popularity to a real readinglog count.
        if entry.enriched_at is None and not hit.weak_signal:
            entry.enriched_at = _utcnow()
            entry.enrich_source = hit.source
    entry.extra = extra
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
    async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
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


# --------------------------------------------------------------------- hot-set seeding
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
    cursor = state.get("cursor") or {"phase": "trending", "i": 0, "offset": 0}

    # Restart a completed pass after a week so trending/popularity stays fresh.
    if cursor.get("phase") == "done":
        last = state.get("last_full_at")
        stale = True
        if last:
            try:
                stale = datetime.fromisoformat(last) < _utcnow() - timedelta(days=7)
            except ValueError:
                stale = True
        if not stale:
            return {"phase": "done", "added": 0}
        cursor = {"phase": "trending", "i": 0, "offset": 0}

    cap = int(cfg["hot_set_cap"])
    count = book_row_count(db)
    added = reqs = 0
    async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
        while reqs < max_requests and cursor.get("phase") != "done":
            phase = cursor["phase"]
            if phase == "trending":
                hits = await _ol_trending(client, _TRENDING[cursor["i"]])
                reqs += 1
                added += _absorb(db, hits)
                cursor["i"] += 1
                if cursor["i"] >= len(_TRENDING):
                    cursor = {"phase": "subjects", "i": 0, "offset": 0}
            elif phase == "subjects":
                if count + added >= cap:
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
                if cursor["offset"] >= _SUBJECT_DEPTH or not ol_hits:
                    cursor["i"] += 1
                    cursor["offset"] = 0
                    if cursor["i"] >= len(_SUBJECTS):
                        cursor = {"phase": "done"}
            db.commit()

    state["cursor"] = cursor
    if cursor.get("phase") == "done":
        state["last_full_at"] = _utcnow().isoformat()
    state["count"] = book_row_count(db)
    _save_state(db, state)
    return {"added": added, "requests": reqs, "phase": cursor.get("phase"), "count": state["count"]}


def _absorb(db: Session, hits: list[BookHit]) -> int:
    return sum(1 for h in hits if _upsert_one(db, h))


def status(db: Session) -> dict:
    st = _state(db)
    return {
        "config": get_config(db),
        "book_rows": book_row_count(db),
        "phase": (st.get("cursor") or {}).get("phase", "idle"),
        "last_full_at": st.get("last_full_at"),
    }
