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
from datetime import UTC, datetime, timedelta

import httpx
from .. import telemetry
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..models import CatalogGroup, CatalogWork

log = logging.getLogger("shelf.indexer")

_GUTENDEX = "https://gutendex.com/books"
_OPENLIBRARY = "https://openlibrary.org/search.json"
_OL_FIELDS = "title,author_name,readinglog_count,ratings_count,ratings_average,subject"
_UA = "Mozilla/5.0 (compatible; ShelfReader/0.1)"

# Bounded work per tick — providers are slow + rate-limited, so keep batches small.
_PER_TICK = 40
_PAUSE_S = 0.3

# When a source pushes back (anti-bot 403 / rate-limit), skip it for this long instead of retrying
# it every ~90s tick. Process-local (a restart re-probes once, then re-cools) — no persistence needed
# since this is a politeness optimization, not correctness. Keyed by ``CatalogWork.domain``; comix.to
# is one domain, so a comix Cloudflare block cools comix alone and the tick keeps enriching every
# OTHER source instead of aborting on the first comix row.
_BLOCK_COOLDOWN_S = 1800
_domain_cooldown: dict[str, datetime] = {}


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _cooling_domains() -> list[str]:
    now = _utcnow()
    return [d for d, until in _domain_cooldown.items() if until > now]


def _is_cooling(domain: str | None) -> bool:
    until = _domain_cooldown.get(domain or "")
    return until is not None and until > _utcnow()


def _cool_domain(domain: str | None) -> None:
    _domain_cooldown[domain or ""] = _utcnow() + timedelta(seconds=_BLOCK_COOLDOWN_S)


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
    # comix rows enrich via AniList (the provider path) — NOT the comix API. comix is only contacted
    # while crawling/indexing, hooking, or refreshing a hooked library item, never on this background
    # enrichment tick. AniList carries the same genres/tags + a better cross-source popularity anyway.
    if d == "gutenberg.org" or d.endswith(".gutenberg.org"):
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
                  demographics=None, fmt: list[dict] | None = None, source: str,
                  adult: bool | None = None, content_type: str | None = None) -> None:
    """Write per-row taxonomy onto extra (the regroup tick rolls these up to the group). ``adult``
    is a provider 18+ flag (AniList isAdult / Google Books MATURE); combined with explicit-adult
    genres it sets ``row.is_adult`` so the Index can gate 18+ content. ``content_type`` (e.g. the
    comix 'manga'/'manhwa' type, or 'book') is the API-derived type the acquisition matchers use to
    reject cross-typed hits — persisted here so it's fetched from the API only once."""
    from . import catalog
    extra = dict(row.extra or {})
    if genres is not None:
        extra["genres"] = genres
    if themes is not None:
        extra["themes"] = themes
    if demographics is not None:
        extra["demographics"] = demographics
    if fmt is not None:
        extra["format"] = fmt
    if content_type:
        extra["content_type"] = content_type
    if adult:
        extra["adult"] = True   # explicit provider flag (sticky once set)
    row.extra = extra
    row.is_adult = catalog.taxonomy_is_adult(extra)
    row.enriched_at = _utcnow()
    row.enrich_source = source


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
    _set_taxonomy(row, genres=genres, themes=themes, source="gutendex", content_type="book")
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


_ENRICH_BACKOFF_BASE_MIN = 30        # 1st transient retry waits 30 min, doubling …
_ENRICH_BACKOFF_MAX_MIN = 24 * 60    # … capped at 24 h
_ENRICH_MAX_ATTEMPTS = 6             # after this many transient failures, give up (stamp it)


def _bump_enrich_backoff(db: Session, row: CatalogWork) -> None:
    """Record an exponential per-row retry delay after a transient enrich failure, so the row isn't
    re-searched every tick. After _ENRICH_MAX_ATTEMPTS it's stamped enriched (gives up; ranks on
    popularity alone) so a permanently-unmatchable title stops being swept. Commits its own row."""
    from datetime import timedelta
    try:
        fresh = db.get(CatalogWork, row.id)
        if fresh is None:
            return
        extra = dict(fresh.extra or {})
        attempts = int(extra.get("enrich_attempts") or 0) + 1
        extra["enrich_attempts"] = attempts
        if attempts >= _ENRICH_MAX_ATTEMPTS:
            extra.pop("enrich_next_at", None)
            fresh.extra = extra
            fresh.enriched_at = _utcnow()
            fresh.enrich_source = fresh.enrich_source or "none"
        else:
            delay = min(_ENRICH_BACKOFF_BASE_MIN * (2 ** (attempts - 1)), _ENRICH_BACKOFF_MAX_MIN)
            extra["enrich_next_at"] = (_utcnow() + timedelta(minutes=delay)).isoformat()
            fresh.extra = extra
        db.commit()
    except Exception:  # noqa: BLE001 — backoff bookkeeping must never break the tick
        db.rollback()


def _persist_identity(row: CatalogWork, provider_kind: str, ref: str | None) -> None:
    """Record the matched provider id as the row's stable identity_key (K1) and in
    extra['enrich_ref'] (so a later re-enrich fetches by id instead of re-searching, 14B). The
    identity_key (e.g. 'anilist:12345') deterministically merges cross-source/cross-language rows
    of the same work in the regroup pass."""
    if not ref:
        return
    ref = str(ref)
    if not row.identity_key:                      # first concrete external id wins (don't churn it)
        row.identity_key = f"{provider_kind}:{ref}"[:64]
    extra = dict(row.extra or {})
    enrich_ref = dict(extra.get("enrich_ref") or {})
    enrich_ref[provider_kind] = ref
    extra["enrich_ref"] = enrich_ref
    row.extra = extra


async def _enrich_provider(client: httpx.AsyncClient, db: Session, row: CatalogWork) -> bool:
    from ..integrations import IntegrationError
    from ..integrations.metadata_sync import MATCH_THRESHOLD, best_match

    _TRANSIENT = object()  # sentinel: this provider failed transiently (down / rate-limited)

    async def _match_fetch(provider):
        """Match + fetch ONE provider. Returns (provider, ref, meta), _TRANSIENT, or None (no match).
        Concurrency-safe: each provider self-throttles on its own ratelimit key."""
        try:
            ref: str | None = None
            # If a prior enrich already matched THIS provider, fetch directly by the stored ref
            # (1 cacheable by-id call) instead of a fresh title search — cuts provider calls on
            # re-enrich (14B). Fall back to a search when there's no stored ref.
            stored = (row.extra or {}).get("enrich_ref") if isinstance(row.extra, dict) else None
            if isinstance(stored, dict):
                ref = stored.get(provider.kind)
            if not ref:
                bm = await best_match(provider, row.title, row.author, row.media_kind)
                if bm is None or bm[0] < MATCH_THRESHOLD:
                    return None
                ref = bm[1].ref
            meta = await provider.fetch(ref)
        except IntegrationError:
            return _TRANSIENT
        except Exception:  # noqa: BLE001
            return None
        if meta is None:
            return None
        return (provider, ref, meta)

    # Query every novel provider CONCURRENTLY and MERGE field-by-field, rather than first-confident-
    # wins (13A): a single provider rarely has everything, so later providers' genres/tags/cover/
    # synopsis/popularity used to be discarded. Order preserved → earlier provider wins ties.
    results = await asyncio.gather(*[_match_fetch(p) for p in _novel_providers()])
    transient = any(r is _TRANSIENT for r in results)
    hits = [r for r in results if isinstance(r, tuple)]

    if hits:
        # ALWAYS record EVERY matched provider id (K1 stable identity + 14B by-id handle) — even a
        # provider with no taxonomy, so the row merges by identity and re-enrich fetches by id.
        for provider, ref, _meta in hits:
            _persist_identity(row, provider.kind, ref)

        genres_union: list[str] = []
        themes_union: list[str] = []
        gseen: set[str] = set()
        tseen: set[str] = set()
        best_pop = 0
        adult = False
        content_type: str | None = None
        sources: list[str] = []
        alias_union: list[str] = []      # provider alternate titles (RanobeDB) → matcher alt_titles
        for provider, _ref, meta in hits:
            for alias in ((meta.extra or {}).get("aliases") or []):
                if alias and alias not in alias_union:
                    alias_union.append(alias)
            for g in (meta.genres or []):                 # genres/aliases → UNION across providers
                if g and g.lower() not in gseen:
                    gseen.add(g.lower())
                    genres_union.append(g)
            for t in (meta.tags or []):
                if t and t.lower() not in tseen:
                    tseen.add(t.lower())
                    themes_union.append(t)
            if isinstance(meta.popularity, int) and meta.popularity > best_pop:
                best_pop = meta.popularity            # popularity → strongest audience signal wins
            if getattr(meta, "is_adult", False):
                adult = True
            mk = getattr(meta, "media_kind", None)        # content_type → 'comic' is the specific win
            if mk == "comic":
                content_type = "comic"
            elif content_type is None and mk == "text":
                content_type = "book"
            if meta.genres or meta.tags:
                sources.append(provider.kind)
            # Fill cover when the row lacks one — OR when its existing cover is a Cloudflare-blocked
            # comix CDN / legacy imgcache URL that won't render: adopt the provider (AniList) cover
            # THIS pass already fetched, so the cover-backfill tick doesn't run a SECOND AniList
            # search for the same cover (F17). Take the LONGEST synopsis (richest description).
            cur_cover = row.cover_url
            if meta.cover_url and (not cur_cover or "comix.to" in cur_cover
                                   or cur_cover.startswith("/media/imgcache")):
                row.cover_url = meta.cover_url
            if meta.synopsis and len(meta.synopsis) > len(row.synopsis or ""):
                row.synopsis = meta.synopsis

        # Persist any provider alternate titles into extra.alt_titles (union, never clobber) so the
        # download matcher + post-download verify score releases/files against the work's romaji/
        # native/synonym titles too — not just its English display title. matchmeta reads this key.
        if alias_union:
            extra = dict(row.extra or {})
            cur = [t for t in (extra.get("alt_titles") or []) if t]
            merged = list(dict.fromkeys([*cur, *alias_union]))
            if merged != cur:
                extra["alt_titles"] = merged
                row.extra = extra

        genres = _tags(genres_union)
        themes = _tags(themes_union)
        if genres or themes:
            ctype = content_type or (
                "book" if (row.media_kind or "text") == "text" else "comic")
            _set_taxonomy(row, genres=genres, themes=themes,
                          source="+".join(dict.fromkeys(sources)) or "novel",
                          adult=adult, content_type=ctype)
            if best_pop > 0:
                row.popularity = float(best_pop)
            return True
        # Matched (identity recorded) but no taxonomy from any novel provider → fall through to OL.

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
    _set_taxonomy(row, genres=_tags(_ol_genres(b.get("subject") or [])), source="openlibrary",
                  content_type="book")
    row.popularity = float(pop)
    avg = b.get("ratings_average")
    if isinstance(avg, (int, float)) and avg > 0:
        row.rating = round(float(avg) * 2.0, 2)  # OL rates 0–5; normalize to the 0–10 convention
    return True


# --------------------------------------------------------------------- tick
async def enrich_catalog_tick(db: Session, *, limit: int = _PER_TICK) -> dict:
    """Enrich up to ``limit`` un-enriched catalog rows, most-popular first. Bounded + polite;
    safe to call repeatedly. Returns a small summary."""
    from sqlalchemy import func
    now_iso = _utcnow().isoformat()
    stmt = (
        select(CatalogWork)
        .where(CatalogWork.enriched_at.is_(None), CatalogWork.hooked_work_id.is_(None))
        .where(or_(CatalogWork.health == "unknown", CatalogWork.health == "ok"))
        # Negative-cache gate (14B): a row in transient-failure backoff isn't due yet — its
        # enrich_next_at is in the future (ISO strings sort chronologically). Rows with no backoff
        # set (the common case) have a NULL extract and pass.
        .where(or_(func.json_extract(CatalogWork.extra, "$.enrich_next_at").is_(None),
                   func.json_extract(CatalogWork.extra, "$.enrich_next_at") <= now_iso))
    )
    # Skip rows from a source that's currently blocking us so a blocked domain (e.g. comix behind
    # Cloudflare) doesn't fill the batch and starve every other source's rows behind it.
    cooling = _cooling_domains()
    if cooling:
        stmt = stmt.where(or_(CatalogWork.domain.is_(None), CatalogWork.domain.notin_(cooling)))
    rows = db.scalars(
        stmt.order_by(CatalogWork.popularity.desc(), CatalogWork.updated_at.desc())
        .limit(max(1, limit))
    ).all()
    if not rows:
        return {"scanned": 0, "enriched": 0}
    enriched = scanned = 0
    async with telemetry.instrument("metadata", timeout=20.0, follow_redirects=True) as client:
        for row in rows:
            # A domain that pushed back earlier THIS tick (or is still cooling) — skip its remaining
            # rows so one struggling source isn't hammered row-by-row.
            if _is_cooling(row.domain):
                continue
            scanned += 1
            strat = _strategy(row.domain)
            try:
                if strat == "gutenberg":
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
                # Upstream is failing (HTTP error / rate-limit / anti-bot 403): don't stamp the row
                # enriched (so it retries later) and put this DOMAIN on cooldown so we neither hammer
                # it 40× this tick nor re-probe it every ~90s. CONTINUE — other sources in the batch
                # can still enrich (a blocked comix must not halt gutenberg/Open Library).
                db.rollback()
                if not _is_cooling(row.domain):
                    log.info("catalog enrich: %s backing off ~%dm — %s",
                             row.domain or "?", _BLOCK_COOLDOWN_S // 60, exc)
                _cool_domain(row.domain)
                # Per-ROW negative cache: the domain cooldown is in-memory + doesn't help when the
                # PROVIDER (AniList/OL) is down rather than the row's source. Persist an exponential
                # backoff so a row that keeps transient-failing isn't re-searched every tick forever;
                # after a cap, stamp it so it stops being swept (it just gets no tags). (14B)
                _bump_enrich_backoff(db, row)
                continue
            except Exception:  # noqa: BLE001 — one bad row shouldn't abort the batch
                db.rollback()
            await asyncio.sleep(_PAUSE_S)
    log.info("catalog enrich: scanned=%s enriched=%s", scanned, enriched)
    return {"scanned": scanned, "enriched": enriched}


# A cover that won't render: none at all, or hosted on comix's Cloudflare-blocked CDN.
# A cover that won't display: none, comix's Cloudflare-blocked CDN, OR a legacy /media/imgcache path
# (the swept cache evicted it and the remote URL was lost — re-source via AniList into durable /covers/).
_COVER_BLOCKED = lambda col: or_(
    col.is_(None), col == "", col.like("%comix.to%"), col.like("/media/imgcache/%"))


async def fetch_cover_via_anilist(db: Session, row, *, force: bool = False) -> str | None:
    """Source a COMIC cover from AniList (whose CDN is reachable, unlike comix's) and store it in the
    DURABLE ``/covers/`` directory, returning the local URL (or None if AniList has no confident
    match). Sets the row's ``cover_url`` to that path so it STICKS — /covers/ is never LRU-evicted,
    unlike the old /media/imgcache path which got swept away under chapter-image churn. With ``force``
    it re-fetches even when a cover is already in place (the manual 'new cover' button)."""
    from .. import imagecache
    from ..integrations.metadata import AniListProvider
    from ..integrations.metadata_sync import best_match
    cur = (getattr(row, "cover_url", None) or "")
    # Keep an already-DURABLE cover (/covers/ or a reachable non-comix remote). Re-source a comix CDN
    # cover or a legacy /media/imgcache one (likely evicted, can't be re-fetched).
    if not force and cur and "comix.to" not in cur and not cur.startswith("/media/imgcache"):
        return cur
    bm = await best_match(AniListProvider(), row.title, getattr(row, "author", None), "comic")
    remote = bm[1].cover_url if bm else None
    if not remote:
        return None
    local = await asyncio.to_thread(imagecache.cache_cover, remote)
    if not local:
        return None
    row.cover_url = local
    db.commit()
    return local


async def backfill_comix_covers(db: Session, *, limit: int = _PER_TICK) -> dict:
    """Sticky comic-cover backfill. comix's own CDN (static.comix.to) is Cloudflare-blocked, so any
    catalog row left with a comix cover (or none) renders blank. For such COMIC rows we look the title
    up on AniList and localize ITS cover — a reachable source. Groups drive the Index display; once a
    row has a localized cover it no longer matches the filter, so it's never re-fetched. Bounded and
    backs off if AniList rate-limits."""
    if _is_cooling("anilist"):
        return {"scanned": 0, "filled": 0, "cooling": True}
    groups = db.scalars(
        select(CatalogGroup)
        .where(_COVER_BLOCKED(CatalogGroup.cover_url), CatalogGroup.media_bucket == "comic")
        .order_by(CatalogGroup.popularity_norm.desc()).limit(max(1, limit))
    ).all()
    filled = scanned = 0
    for g in groups:
        scanned += 1
        try:
            got = await fetch_cover_via_anilist(db, g)
        except Exception as exc:  # noqa: BLE001 — AniList unreachable / rate-limited
            db.rollback()
            log.info("cover backfill: anilist backing off ~%dm — %s", _BLOCK_COOLDOWN_S // 60, exc)
            _cool_domain("anilist")
            break
        if got:
            filled += 1
        await asyncio.sleep(_PAUSE_S)
    log.info("cover backfill (anilist): scanned=%s filled=%s", scanned, filled)
    return {"scanned": scanned, "filled": filled}
