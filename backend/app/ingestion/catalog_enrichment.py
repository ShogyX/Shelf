"""Enrich catalog rows with genres / themes / popularity so the Index page can build
category rows.

The catalog is discovered by crawling, which yields title/author/cover/synopsis but no
taxonomy. This bounded background tick fills that in, **popular-first**, with a per-domain
strategy (so we use the richest cheap source for each site):

  * comix.to    → its per-title JSON API for genres/tags/demographics/format, but POPULARITY
                  from AniList (comix's own follow count over-represents its manhwa-heavy
                  audience); falls back to the comix follow count only when AniList has no match.
  * gutenberg   → the gutendex API (subjects → genres, bookshelves → themes, download_count
                  → popularity).
  * everything else (text) → the metadata providers: AniList/ranobedb for light novels/manga,
                  then **Open Library** (``_enrich_openlibrary``) for MAINSTREAM PROSE BOOKS,
                  whose ``readinglog_count`` is their audience signal.

## Popularity model (cross-source ranking — read before adding a source) ##############
Each row carries a RAW ``CatalogWork.popularity`` from a source-specific signal that is NOT
comparable across sources (AniList user counts ~hundreds of thousands; Open Library reading-log
counts ~thousands; gutendex downloads; comix follows). The single authority per medium is used —
e.g. comics rank by AniList's GLOBAL user count, not comix's local follows.

Cross-source comparability is achieved in :mod:`app.ingestion.catalog_groups`.
``_normalize_popularity`` converts raw popularity to ``CatalogGroup.popularity_norm`` (0..1) as a
PERCENTILE RANK WITHIN each (source_domain, media_bucket). That is the ranking key the Index rows
use — so the top of any source maps to ~1.0 and a new source's titles interleave fairly with
existing ones regardless of its raw scale.

**Forward-looking — mainstream books:** when book titles are added (their own source/site), they
enrich via Open Library and rank on the SAME normalized 0..1 scale as AniList-ranked manga. To add
another popularity source, give it an enrich strategy that sets ``row.popularity`` + an
``enrich_source`` tag; the per-source normalization makes it rank-comparable automatically — do
NOT mix raw scales into one pool.
#######################################################################################

Per-row genres/themes are stashed on ``CatalogWork.extra`` (``genres``/``themes``/
``demographics``/``format`` lists of ``{slug,label}``); the regroup tick
(:mod:`app.ingestion.catalog_groups`) rolls them up into deduped ``CatalogTag`` rows on the
group. Bounded per call and rate-aware so it never floods a source or blocks the loop.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import UTC, datetime

import httpx
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..models import CatalogWork

log = logging.getLogger("shelf.indexer")

_GUTENDEX = "https://gutendex.com/books"
_COMIX_DETAIL = "https://comix.to/api/v1/manga"
_OPENLIBRARY = "https://openlibrary.org/search.json"
_OL_FIELDS = "title,author_name,readinglog_count,ratings_count,ratings_average,subject"
_UA = "Mozilla/5.0 (compatible; ShelfReader/0.1)"

# Bounded work per tick — providers are slow + rate-limited, so keep batches small.
_PER_TICK = 40
_PAUSE_S = 0.3


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").strip().lower()).strip("-")[:96]


def _tags(labels: list[str], cap: int = 8) -> list[dict]:
    """Dedupe + slugify a list of human labels into ``[{slug,label}]`` (capped)."""
    out: list[dict] = []
    seen: set[str] = set()
    for lab in labels:
        lab = (lab or "").strip()
        sg = _slug(lab)
        if not sg or sg in seen:
            continue
        seen.add(sg)
        out.append({"slug": sg, "label": lab[:96]})
        if len(out) >= cap:
            break
    return out


def _strategy(domain: str) -> str:
    d = (domain or "").lower()
    if d.startswith("www."):
        d = d[4:]
    if d == "comix.to" or d.endswith(".comix.to"):
        return "comix"
    if d.endswith("gutenberg.org"):
        return "gutenberg"
    return "provider"


def _comix_hid(row: CatalogWork) -> str | None:
    """comix work URLs are /title/<hid>-<slug>; the hid is also stashed on extra by the
    list ingest. Fall back to parsing the URL for rows ingested before that."""
    hid = (row.extra or {}).get("hid")
    if hid:
        return str(hid)
    m = re.search(r"/title/([^/?#]+)", row.work_url or "")
    if not m:
        return None
    return m.group(1).split("-", 1)[0] or None


def _gutenberg_id(row: CatalogWork) -> str | None:
    m = re.search(r"/ebooks/(\d+)", row.work_url or "")
    return m.group(1) if m else None


class _Transient(Exception):
    """Upstream failed transiently (HTTP error / network / rate-limit) — leave the row un-stamped
    so it's retried later, and stop the tick so a failing API isn't hammered every row."""


def _set_taxonomy(row: CatalogWork, *, genres=None, themes=None,
                  demographics=None, fmt: list[dict] | None = None, source: str) -> None:
    """Write per-row taxonomy onto extra (the regroup tick rolls these up to the group)."""
    extra = dict(row.extra or {})
    if genres is not None:
        extra["genres"] = genres
    if themes is not None:
        extra["themes"] = themes
    if demographics is not None:
        extra["demographics"] = demographics
    if fmt is not None:
        extra["format"] = fmt
    row.extra = extra
    row.enriched_at = _utcnow()
    row.enrich_source = source


async def _anilist_popularity(row: CatalogWork) -> int | None:
    """Authoritative GLOBAL popularity (AniList user count) for a comic title, or None when AniList
    has no confident match. comix's own follow count over-represents its manhwa-heavy audience, so
    we rank by AniList's cross-source signal instead (e.g. One Piece ~225k ≫ a niche manhwa ~1k).
    Raises ``_Transient`` if AniList is unavailable/rate-limited so the row is retried, not stamped
    with the weaker comix signal."""
    from ..integrations import IntegrationError
    from ..integrations.metadata import AniListProvider
    from ..integrations.metadata_sync import MATCH_THRESHOLD, best_match

    provider = AniListProvider()
    try:
        bm = await best_match(provider, row.title, row.author, row.media_kind)
        if bm is None or bm[0] < MATCH_THRESHOLD:
            return None  # genuinely not on AniList → caller falls back to the comix follow count
        meta = await provider.fetch(bm[1].ref)
    except IntegrationError as exc:
        raise _Transient(f"anilist: {exc}") from exc
    if meta is not None and isinstance(meta.popularity, int) and meta.popularity > 0:
        return meta.popularity
    return None


# --------------------------------------------------------------------- comix
async def _enrich_comix(client: httpx.AsyncClient, db: Session, row: CatalogWork) -> bool:
    from .netguard import BlockedAddress, assert_public_url

    hid = _comix_hid(row)
    if not hid:
        return False
    url = f"{_COMIX_DETAIL}/{hid}"
    try:
        await asyncio.to_thread(assert_public_url, url)
    except BlockedAddress:
        return False
    try:
        r = await client.get(url, headers={"Accept": "application/json", "Origin": "https://comix.to",
                                           "User-Agent": _UA})
    except httpx.HTTPError as exc:
        raise _Transient(f"comix: {exc}") from exc
    if r.status_code != 200:
        raise _Transient(f"comix HTTP {r.status_code}")
    try:
        item = (r.json() or {}).get("result")
    except Exception:  # noqa: BLE001
        return False
    if not isinstance(item, dict):
        return False
    # Backfill a missing cover from the comix poster (the detail fetch already gives it) — some
    # catalog rows were ingested without one and otherwise render coverless on the Index.
    if not (row.cover_url or "").strip():
        poster = item.get("poster") if isinstance(item.get("poster"), dict) else {}
        cover = (poster or {}).get("large") or (poster or {}).get("medium")
        if cover:
            row.cover_url = cover
    genres = _tags([g.get("title") for g in (item.get("genres") or []) if isinstance(g, dict)])
    themes = _tags([t.get("title") for t in (item.get("tags") or []) if isinstance(t, dict)])
    demos = _tags([d.get("title") for d in (item.get("demographics") or []) if isinstance(d, dict)])
    fmt = _tags([item.get("type")]) if item.get("type") else []
    _set_taxonomy(row, genres=genres, themes=themes, demographics=demos, fmt=fmt, source="comix")
    # Popularity: prefer AniList's authoritative GLOBAL audience count over comix's own follow
    # count (comix skews manhwa, so its follows over-rank webtoons vs famous manga). Fall back to
    # the comix follow count only when AniList has no match for the title.
    follows = item.get("followsTotal")
    ext = await _anilist_popularity(row)
    if ext is not None:
        row.popularity = float(ext)
        row.enrich_source = "comix+anilist"
    elif isinstance(follows, (int, float)) and follows >= 0:
        row.popularity = float(follows)
    rated = item.get("ratedAvg")
    if isinstance(rated, (int, float)) and rated > 0:
        row.rating = float(rated)
    return True


# --------------------------------------------------------------------- gutenberg
def _gutenberg_genres(subjects: list[str]) -> list[str]:
    """Project messy LCSH subjects ('Detective and mystery stories -- England -- Fiction')
    onto their leading facet so they cluster into usable genre buckets."""
    out: list[str] = []
    for s in subjects or []:
        head = (s or "").split(" -- ")[0].strip()
        if head and head.lower() != "fiction":
            out.append(head)
    return out


async def _enrich_gutenberg(client: httpx.AsyncClient, db: Session, row: CatalogWork) -> bool:
    from .netguard import BlockedAddress, assert_public_url

    gid = _gutenberg_id(row)
    if not gid:
        return False
    url = f"{_GUTENDEX}/{gid}/"  # trailing slash — gutendex 301s the slashless form
    try:
        await asyncio.to_thread(assert_public_url, url)
    except BlockedAddress:
        return False
    try:
        r = await client.get(url, headers={"Accept": "application/json", "User-Agent": _UA})
    except httpx.HTTPError as exc:
        raise _Transient(f"gutendex: {exc}") from exc
    if r.status_code != 200:
        raise _Transient(f"gutendex HTTP {r.status_code}")
    try:
        item = r.json()
    except Exception:  # noqa: BLE001
        return False
    if not isinstance(item, dict) or not item.get("id"):
        return False
    genres = _tags(_gutenberg_genres(item.get("subjects") or []))
    themes = _tags(item.get("bookshelves") or [])
    _set_taxonomy(row, genres=genres, themes=themes, source="gutendex")
    dl = item.get("download_count")
    if isinstance(dl, (int, float)) and dl >= 0:
        row.popularity = float(dl)
    return True


# --------------------------------------------------------------------- providers (novels)
def _novel_providers():
    """Keyless providers that carry genres/tags for prose works, richest first. AniList covers
    light novels (format NOVEL under type MANGA) with genres+tags+popularity; ranobedb is a
    light-novel specialist fallback."""
    from ..integrations.metadata import AniListProvider, RanobeDbProvider
    return [AniListProvider(), RanobeDbProvider()]


async def _enrich_provider(client: httpx.AsyncClient, db: Session, row: CatalogWork) -> bool:
    from ..integrations import IntegrationError
    from ..integrations.metadata_sync import MATCH_THRESHOLD, best_match

    transient = False
    for provider in _novel_providers():
        try:
            bm = await best_match(provider, row.title, row.author, row.media_kind)
            if bm is None or bm[0] < MATCH_THRESHOLD:
                continue
            meta = await provider.fetch(bm[1].ref)
        except IntegrationError:
            transient = True  # provider down / rate-limited — try the next, or retry next tick
            continue
        except Exception:  # noqa: BLE001
            continue
        if meta is None:
            continue
        genres = _tags(meta.genres)
        themes = _tags(meta.tags)
        if not genres and not themes:
            continue  # a match with no taxonomy isn't worth marking enriched — let another try
        _set_taxonomy(row, genres=genres, themes=themes, source=provider.kind)
        if isinstance(meta.popularity, int) and meta.popularity > 0:
            row.popularity = float(meta.popularity)
        return True
    # AniList/ranobedb are light-novel/manga specialists; they miss MAINSTREAM PROSE BOOKS. Open
    # Library carries those with a reading-log audience count — the popularity signal that lets
    # future book titles rank comparably to manga (see the module 'Popularity model' note).
    if await _enrich_openlibrary(client, db, row):
        return True
    if transient:  # every provider was unavailable → retry the row next tick, don't stamp it
        raise _Transient("all novel providers unavailable")
    return False


# --------------------------------------------------------------------- open library (books)
def _ol_genres(subjects: list[str]) -> list[str]:
    """Pull usable genre-ish labels out of Open Library's noisy subject list (drop place/era/
    award noise like 'Dune (Imaginary place)' or 'New York Times reviewed')."""
    out: list[str] = []
    for s in subjects or []:
        s = (s or "").strip()
        low = s.lower()
        if (not s or len(s) > 28 or any(ch in s for ch in "(),:=") or s[:1].isdigit()
                or low in ("fiction", "general", "nyt", "new york times reviewed")):
            continue
        out.append(s)
    return out


async def _enrich_openlibrary(client: httpx.AsyncClient, db: Session, row: CatalogWork) -> bool:
    """Mainstream-book popularity + genres from Open Library's free search API. ``readinglog_count``
    (how many people have the book on a shelf) is the cross-source audience signal; ``subject``
    gives genres. Returns False on no confident title match (so the row stays a low-ranked miss)."""
    from urllib.parse import quote_plus

    from .extract import norm_title
    from .netguard import BlockedAddress, assert_public_url

    title = (row.title or "").strip()
    if not title:
        return False
    q = f"title={quote_plus(title)}" + (f"&author={quote_plus(row.author)}" if row.author else "")
    url = f"{_OPENLIBRARY}?{q}&limit=1&fields={_OL_FIELDS}"
    try:
        await asyncio.to_thread(assert_public_url, url)
    except BlockedAddress:
        return False
    try:
        r = await client.get(url, headers={"Accept": "application/json", "User-Agent": _UA})
    except httpx.HTTPError as exc:
        raise _Transient(f"openlibrary: {exc}") from exc
    if r.status_code != 200:
        raise _Transient(f"openlibrary HTTP {r.status_code}")
    try:
        docs = (r.json() or {}).get("docs") or []
    except Exception:  # noqa: BLE001
        return False
    if not docs:
        return False
    b = docs[0]
    # Title guard — Open Library search is loose, so reject a wildly-wrong first hit.
    ta, tb = set(norm_title(title).split()), set(norm_title(b.get("title") or "").split())
    if not ta or not tb or len(ta & tb) / len(ta | tb) < 0.6:
        return False
    pop = b.get("readinglog_count")
    if not isinstance(pop, (int, float)) or pop <= 0:
        return False  # no audience signal → not worth marking enriched; let it stay a low miss
    _set_taxonomy(row, genres=_tags(_ol_genres(b.get("subject") or [])), source="openlibrary")
    row.popularity = float(pop)
    avg = b.get("ratings_average")
    if isinstance(avg, (int, float)) and avg > 0:
        row.rating = round(float(avg) * 2.0, 2)  # OL rates 0–5; normalize to the 0–10 convention
    return True


# --------------------------------------------------------------------- tick
async def enrich_catalog_tick(db: Session, *, limit: int = _PER_TICK) -> dict:
    """Enrich up to ``limit`` un-enriched catalog rows, most-popular first. Bounded + polite;
    safe to call repeatedly. Returns a small summary."""
    rows = db.scalars(
        select(CatalogWork)
        .where(CatalogWork.enriched_at.is_(None), CatalogWork.hooked_work_id.is_(None))
        .where(or_(CatalogWork.health == "unknown", CatalogWork.health == "ok"))
        .order_by(CatalogWork.popularity.desc(), CatalogWork.updated_at.desc())
        .limit(max(1, limit))
    ).all()
    if not rows:
        return {"scanned": 0, "enriched": 0}
    enriched = scanned = 0
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
        for row in rows:
            scanned += 1
            strat = _strategy(row.domain)
            try:
                if strat == "comix":
                    ok = await _enrich_comix(client, db, row)
                elif strat == "gutenberg":
                    ok = await _enrich_gutenberg(client, db, row)
                else:
                    ok = await _enrich_provider(client, db, row)
                if ok:
                    enriched += 1
                else:
                    # A definitive miss (200 response, but no taxonomy exists for this title):
                    # mark attempted so a permanently-unmatchable row doesn't re-block the queue
                    # every tick; it just gets no tags (and ranks on popularity alone).
                    row.enriched_at = _utcnow()
                    row.enrich_source = row.enrich_source or "none"
                db.commit()
            except _Transient as exc:
                # Upstream is failing (HTTP error / rate-limit): don't stamp the row (so it retries)
                # and stop the tick so we don't hammer a struggling API 40× — next tick (~90s) retries.
                db.rollback()
                log.info("catalog enrich: backing off — %s", exc)
                break
            except Exception:  # noqa: BLE001 — one bad row shouldn't abort the batch
                db.rollback()
            await asyncio.sleep(_PAUSE_S)
    log.info("catalog enrich: scanned=%s enriched=%s", scanned, enriched)
    return {"scanned": scanned, "enriched": enriched}


async def backfill_comix_covers(db: Session, *, limit: int = _PER_TICK) -> dict:
    """Cover-only backfill: fill comix catalog rows that ended up WITHOUT a cover (so they don't
    render coverless on the Index). Cheap — one comix detail fetch per row, just for the poster, no
    AniList/taxonomy work — and bounded; the most popular coverless rows are filled first."""
    from .netguard import BlockedAddress, assert_public_url
    rows = db.scalars(
        select(CatalogWork)
        .where(or_(CatalogWork.cover_url.is_(None), CatalogWork.cover_url == ""))
        .where(CatalogWork.domain == "comix.to")
        .order_by(CatalogWork.popularity.desc())
        .limit(max(1, limit))
    ).all()
    filled = scanned = 0
    if not rows:
        return {"scanned": 0, "filled": 0}
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
        for row in rows:
            hid = _comix_hid(row)
            if not hid:
                continue
            scanned += 1
            url = f"{_COMIX_DETAIL}/{hid}"
            try:
                await asyncio.to_thread(assert_public_url, url)
                r = await client.get(url, headers={"Accept": "application/json",
                                                   "Origin": "https://comix.to", "User-Agent": _UA})
                if r.status_code != 200:
                    raise _Transient(f"comix HTTP {r.status_code}")
                item = (r.json() or {}).get("result")
            except (BlockedAddress, ValueError):
                continue
            except (httpx.HTTPError, _Transient) as exc:
                log.info("comix cover backfill: backing off — %s", exc)
                break  # upstream struggling → stop; next tick retries
            poster = item.get("poster") if isinstance(item, dict) and isinstance(item.get("poster"), dict) else {}
            cover = (poster or {}).get("large") or (poster or {}).get("medium")
            if cover:
                row.cover_url = cover
                db.commit()
                filled += 1
            await asyncio.sleep(_PAUSE_S)
    log.info("comix cover backfill: scanned=%s filled=%s", scanned, filled)
    return {"scanned": scanned, "filled": filled}
