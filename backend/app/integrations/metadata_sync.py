"""Match library works to metadata providers and enrich them (source of truth).

Pass 1: search a provider for each hooked Work, pick the best title+author match above a
confidence threshold, record a :class:`MetadataLink`, and overwrite the work's displayed
metadata (author / synopsis / cover / expected release count) from the provider. The reader
still pulls chapters from the work's original source — only the *metadata* comes from here.
Release-detection, related-series, and the Goodreads wishlist build on these links (pass 2).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import imagecache
from . import IntegrationError
from ..ingestion.extract import authors_compatible, norm_title
from ..models import (
    Bookshelf,
    CatalogWork,
    LibraryItem,
    MetadataLink,
    QueuedHook,
    Work,
)
from .metadata import MetadataProvider, ProviderMatch, ProviderMeta

log = logging.getLogger("shelf.metadata")

MATCH_THRESHOLD = 0.6  # below this we don't auto-link (avoid wrong source-of-truth)
MAX_HOOK_ATTEMPTS = 5  # give up auto-hooking a queued title after this many genuine failures

# A candidate whose title adds one of these tokens (that the work's title lacks) is a *companion*
# product — a fanbook / artbook / anthology — not the work itself. Linking one would feed a wrong
# chapter/volume count into the source-of-truth, so it's rejected outright (see _confidence).
_COMPANION_TOKENS = frozenset({
    "fanbook", "fanbooks", "artbook", "artbooks", "databook", "guidebook", "sourcebook",
    "anthology", "artworks", "illustrations", "coloring", "colouring", "handbook",
    "encyclopedia", "companion", "doujin", "doujinshi", "fanmade", "novelization",
})


def _confidence(work_title: str, work_author: str | None, m: ProviderMatch,
                work_media_kind: str | None = None, *, require_author: bool = False) -> float:
    """How confidently a provider match is the same work — normalized title overlap, gated
    by author compatibility so a same-titled different work doesn't get linked, and by medium
    so a comic ADAPTATION (e.g. the Reverend Insanity manhua, with its own much smaller chapter
    count) is never linked to the prose novel as if they were the same work.

    A title that only PARTIALLY overlaps is trusted ONLY when both authors are known and
    compatible — without that corroboration a partial overlap is just a same-word collision (e.g.
    the web-novel 'Against the Gods' wrongly matching 'God Against the Gods' by Jonathan Kirsch).
    ``require_author`` additionally demands corroborating authors for an EXACT title too — used for
    Google Books, whose huge single-edition catalog is full of unrelated same-titled books."""
    a, b = norm_title(work_title), norm_title(m.title)
    if not a or not b:
        return 0.0
    ta, tb = set(a.split()), set(b.split())
    # Reject companion editions: a candidate carrying a fanbook/artbook/… token the work doesn't.
    if _COMPANION_TOKENS & (tb - ta):
        return 0.0
    authors_known = bool((work_author or "").strip() and (m.author or "").strip())
    compat = authors_compatible(work_author, m.author)
    if a == b:
        if not compat:
            score = 0.55  # same title but conflicting known authors → probably a different work
        elif require_author and not authors_known:
            return 0.0  # provider needs author corroboration and we have none → don't link
        else:
            score = 1.0
    elif len(ta) < 2 or len(tb) < 2:
        return 0.0
    else:
        # Partial overlap: only believable when the AUTHORS corroborate it (without that, it's a
        # same-word collision — e.g. 'Against the Gods' vs 'God Against the Gods').
        if not (authors_known and compat):
            return 0.0
        score = len(ta & tb) / len(ta | tb)
        # A dropped subtitle / edition makes one title a strict SUBSET of the other (provider 'X' vs
        # work 'X and the Story of Y' — common for library/Gutenberg titles with long subtitles).
        # Symmetric Jaccard unfairly halves that; with corroborating authors it's a confident match,
        # so lift it over the threshold. Capped (not 1.0) since same-author sequels can also nest, and
        # an exact-title candidate, when one exists, still outscores this.
        if ta < tb or tb < ta:
            score = max(score, 0.7)
    # Different medium → almost certainly a separate work (novel vs its manhua); a prose novel and
    # its comic adaptation have DIFFERENT chapter counts, so they must not become one another's
    # source of truth. Drop the score hard so even an exact title match falls below the threshold.
    if work_media_kind and m.media_kind and work_media_kind != m.media_kind:
        score *= 0.4
    return score


def _clean_query(title: str) -> str:
    """Strip reader-site chrome and series/volume suffixes off a catalog title so the provider
    search isn't poisoned by '… - Chapter 5 | ReadNovelFree' style noise. Used only to widen
    the SEARCH; matches are still scored against the original title, so precision is unchanged."""
    import re
    t = re.sub(r"\s*[|｜–—-]\s*(read|chapter|ch\.?|vol(?:ume)?|novel|manga|manhwa|manhua|"
               r"webtoon|webnovel|light\s*novel)\b.*$", "", title or "", flags=re.I)
    t = re.sub(r"\s*\([^)]*#[^)]*\)\s*$", "", t)            # '(Series, #3)'
    t = re.sub(r"[,:]?\s*(vol(?:ume)?\.?|book|part)\s*\d+.*$", "", t, flags=re.I)
    return t.strip()


# Providers whose catalog is a vast pool of single, unrelated same-titled editions (no series
# tracking) — a title-only match there is unreliable, so we demand corroborating authors.
_AUTHOR_REQUIRED_PROVIDERS = frozenset({"googlebooks"})


async def _best_for_query(provider: MetadataProvider, query: str, score_title: str,
                          author: str | None, media_kind: str | None = None
                          ) -> tuple[float, ProviderMatch] | None:
    matches = await provider.search(query, author)
    need_author = provider.kind in _AUTHOR_REQUIRED_PROVIDERS
    scored = sorted(
        ((_confidence(score_title, author, m, media_kind, require_author=need_author), m)
         for m in matches),
        key=lambda x: x[0], reverse=True)
    return scored[0] if scored else None


async def best_match(provider: MetadataProvider, title: str, author: str | None,
                     media_kind: str | None = None) -> tuple[float, ProviderMatch] | None:
    bm = await _best_for_query(provider, title, title, author, media_kind)
    if bm is not None and bm[0] >= MATCH_THRESHOLD:
        return bm
    # First query missed — retry once with a cleaned title, scoring against that cleaned title
    # too (the noise we stripped, e.g. '… - Chapter 12 | Site', otherwise dilutes the score of
    # an otherwise-perfect candidate). The clean is conservative, so this widens recall without
    # inviting wrong matches.
    alt_q = _clean_query(title)
    if alt_q and alt_q.lower() != (title or "").lower():
        alt = await _best_for_query(provider, alt_q, alt_q, author, media_kind)
        if alt is not None and (bm is None or alt[0] > bm[0]):
            return alt
    return bm


async def _resolve_cover(meta: ProviderMeta) -> str | None:
    """Cache the provider's cover off the event loop. If the (full-res) cover URL resolved to a
    rejected placeholder (PERMANENT_FAIL — e.g. Google Books' 'image not available'), fall back to
    the provider's plain thumbnail (``extra['cover_thumb']``) so a real, if lower-res, cover is
    still shown rather than nothing."""
    if not meta.cover_url:
        return None
    local = await asyncio.to_thread(imagecache.cache_image, meta.cover_url)
    thumb = (meta.extra or {}).get("cover_thumb")
    if local == imagecache.PERMANENT_FAIL and thumb and thumb != meta.cover_url:
        local = await asyncio.to_thread(imagecache.cache_image, thumb)
    return local


# A provider's authoritative fine media label (AniList's `format` is the only granular one). Used to
# OVERRIDE the URL/title heuristic so an enriched work is categorized by what metadata proves it is.
_FORMAT_LABEL = {
    "MANGA": "Manga", "MANHWA": "Webtoon", "MANHUA": "Manhua", "ONE_SHOT": "Manga",
    "OEL": "Comic", "NOVEL": "Novel", "LIGHT_NOVEL": "Novel",
}
_COMIC_LABELS = ("Manga", "Manhua", "Webtoon", "Comic")


def _meta_label(meta: ProviderMeta) -> str | None:
    """The authoritative fine media label from a provider's metadata, if it carries one."""
    return _FORMAT_LABEL.get(((meta.extra or {}).get("format") or "").upper())


def _apply_meta_label(db: Session, work: Work, meta: ProviderMeta) -> None:
    """Persist the provider's authoritative label onto the catalog entries this work was hooked from,
    so the Index category badge reflects metadata's verdict (not the URL/title guess). A comic label
    also flips media_kind so the grouping bucket agrees with the badge."""
    label = _meta_label(meta)
    if not label:
        return
    from ..models import CatalogWork
    for cw in db.scalars(select(CatalogWork).where(CatalogWork.hooked_work_id == work.id)).all():
        ex = dict(cw.extra or {})
        if ex.get("meta_label") != label:
            ex["meta_label"] = label
            cw.extra = ex
        if label in _COMIC_LABELS and cw.media_kind != "comic":
            cw.media_kind = "comic"
    if label in _COMIC_LABELS and work.media_kind != "comic":
        work.media_kind = "comic"


def enrich_work(db: Session, work: Work, meta: ProviderMeta, cover_local: str | None = None,
                provider_kind: str | None = None) -> None:
    """Overwrite the work's displayed metadata from the provider (the source of truth).
    Only non-empty provider fields are applied. ``cover_local`` is a pre-resolved cached
    cover URL (caching is done off the event loop by the caller)."""
    # Upgrade-not-clobber (MERGE-1), mirroring the catalog-discovery precedence: fill author/cover
    # only when currently empty, and keep the LONGER synopsis. Without this a broad provider (Google
    # Books) run after a specialist (RanobeDB/AniList) would overwrite the richer specialist data.
    if meta.author and not work.author:
        work.author = meta.author[:255]
    if meta.synopsis and len(meta.synopsis) > len(work.description or ""):
        work.description = meta.synopsis
    if cover_local and cover_local != imagecache.PERMANENT_FAIL and not work.cover_url:
        work.cover_url = cover_local
    elif meta.cover_url and cover_local is None and not work.cover_url:
        work.cover_url = meta.cover_url  # caller didn't cache; use the remote URL
    # Only a CHAPTER-unit count may drive the crawl/completeness target, and never LOWER it.
    # A provider VOLUME count (ranobedb) is kept on the MetadataLink, not written here —
    # writing 33 volumes onto a 2000-chapter work would corrupt health + stop the backfill.
    if meta.total_units and meta.unit_kind == "chapters":
        work.total_chapters_expected = max(work.total_chapters_expected or 0, meta.total_units)
    if meta.media_kind == "comic" and work.media_kind != "comic":
        work.media_kind = "comic"
    _apply_meta_label(db, work, meta)   # authoritative fine label → catalog badge (overrides heuristic)
    _apply_display_meta(work, meta, provider_kind)


def _apply_display_meta(work: Work, meta: ProviderMeta, provider_kind: str | None = None) -> None:
    """Fill the detail-modal display columns (rating/year/genres/publisher/pages/identifiers) from a
    provider, upgrade-not-clobber: only set an empty field (specialists run first, so the first
    provider with a value wins) and keep the larger rating sample. Stamps meta_enriched_at/source so
    the backfill tick stops sweeping this work."""
    extra = meta.extra or {}
    if work.rating is None and isinstance(meta.rating, (int, float)):
        # Take score + sample together so the displayed "8.8 (1,200)" pair always comes from ONE
        # provider (never provider A's score beside provider B's count).
        work.rating = float(meta.rating)
        work.rating_count = meta.rating_count
    if work.year is None and isinstance(meta.year, int):
        work.year = meta.year
    if not work.publisher and meta.publisher:
        work.publisher = meta.publisher[:255]
    if work.page_count is None:
        pages = extra.get("page_count") or (meta.total_units if meta.unit_kind == "pages" else None)
        if isinstance(pages, int) and pages > 0:
            work.page_count = pages
    if not work.genres and meta.genres:
        work.genres = [g for g in meta.genres if g][:12]
    isbns = [i for i in (extra.get("isbn") or []) if i]
    if isbns or (provider_kind and meta.ref):
        ids = dict(work.identifiers or {})
        if isbns:
            ids["isbn"] = sorted({*(ids.get("isbn") or []), *isbns})
        if provider_kind and meta.ref:
            ids.setdefault(provider_kind, str(meta.ref))
        work.identifiers = ids
    work.meta_enriched_at = datetime.now(UTC)
    if provider_kind:
        work.meta_source = (f"{work.meta_source}+{provider_kind}" if work.meta_source
                            and provider_kind not in work.meta_source else
                            work.meta_source or provider_kind)[:64]


async def reconcile_chapter_count(db: Session, work: Work, meta: ProviderMeta, *,
                                  trigger: bool = True) -> bool:
    """Treat a CHAPTER-reporting provider (NovelUpdates / AniList) as the source of truth for how
    many chapters a title has. When it reports MORE chapters than we've downloaded, raise the
    expected ceiling and (when ``trigger``) nudge the work's own reading source to discover +
    enqueue the missing chapters — the provider only knows the *count*; the chapters themselves
    still come from the source. Returns True if a source fetch was triggered.

    ``trigger=False`` raises the target only (used right after a hook, where the source was just
    discovered — an immediate re-discovery wouldn't surface anything new). No-op for volume/page
    providers, and the raise-only rule means a (mis-)matched provider can never LOWER a real
    target. Callers gate re-runs (e.g. only when the provider's marker advanced) so a permanently
    behind work — one whose source genuinely can't reach the provider's count — isn't re-crawled
    on every sweep."""
    if meta.unit_kind != "chapters" or not meta.total_units:
        return False
    # Defense-in-depth: only a SAME-medium count is comparable. A comic adaptation's chapter
    # count must never drive a prose work's target (matching already gates this, but a directly
    # supplied / confirmed link could bypass it).
    if meta.media_kind and work.media_kind and meta.media_kind != work.media_kind:
        return False
    known = work.total_chapters_known or 0
    if meta.total_units <= known:
        return False
    work.total_chapters_expected = max(work.total_chapters_expected or 0, meta.total_units)
    db.commit()
    if not trigger:
        return False
    from ..ingestion import tracker
    try:
        await tracker.check_work(db, work)  # discovers + enqueues whatever the source now exposes
    except Exception:  # noqa: BLE001 — discovery is best-effort; the raised target still stands
        db.rollback()
        return False
    log.info("chapter-truth: work=%s %s says %s chapters > %s downloaded → triggered fetch",
             work.id, meta.unit_kind, meta.total_units, known)
    return True


def upsert_link(db: Session, work: Work, provider_kind: str, meta: ProviderMeta,
                confidence: float, status: str = "auto") -> MetadataLink:
    link = db.scalar(
        select(MetadataLink).where(
            MetadataLink.work_id == work.id, MetadataLink.provider == provider_kind
        )
    )
    if link is None:
        link = MetadataLink(work_id=work.id, provider=provider_kind)
        db.add(link)
    link.ref = meta.ref
    link.matched_title = meta.title[:512]
    link.confidence = round(confidence, 3)
    if link.status != "confirmed":  # don't downgrade an operator-confirmed link
        link.status = status
    link.release_marker = meta.release_marker
    link.total_units = meta.total_units
    link.unit_kind = meta.unit_kind
    link.payload = {
        "url": meta.url,
        "status": meta.status,
        "related": [{"title": r.title, "relation": r.relation, "ref": r.ref} for r in meta.related],
        **(meta.extra or {}),
    }
    link.last_checked_at = datetime.now(UTC)
    return link


async def match_and_enrich_work(db: Session, work: Work, provider: MetadataProvider,
                                *, trigger_fetch: bool = True) -> MetadataLink | None:
    """Find + link the best provider match for one work and enrich it. Returns the link
    (or None if nothing matched confidently). ``trigger_fetch=False`` skips the immediate
    source re-discovery on a chapter mismatch (used right after a hook)."""
    bm = await best_match(provider, work.title, work.author, work.media_kind)
    if bm is None or bm[0] < MATCH_THRESHOLD:
        return None
    confidence, match = bm
    meta = await provider.fetch(match.ref)
    if meta is None:
        return None
    # Cache the cover off the event loop (blocking DNS + download), with placeholder fallback.
    cover_local = await _resolve_cover(meta)
    link = upsert_link(db, work, provider.kind, meta, confidence)
    enrich_work(db, work, meta, cover_local, provider_kind=provider.kind)
    db.commit()
    log.info("linked work=%s -> %s:%s (conf=%.2f)", work.id, provider.kind, meta.ref, confidence)
    # If this provider reports chapters and we're behind its count, fetch the missing chapters now
    # (first-time catch-up). On the hook path the source was just discovered, so only raise target.
    await reconcile_chapter_count(db, work, meta, trigger=trigger_fetch)
    return link


async def enrich_library(db: Session, provider: MetadataProvider, *, limit: int = 40,
                         relink: bool = False) -> dict:
    """Match + enrich hooked works that don't yet have a link for this provider (or all,
    when relink=True). Bounded per call; returns a summary."""
    linked_ids = {
        wid for (wid,) in db.execute(
            select(MetadataLink.work_id).where(MetadataLink.provider == provider.kind)
        ).all()
    }
    sel = select(Work).where(Work.hooked.is_(True)).order_by(Work.id)
    matched = scanned = 0
    error: str | None = None
    for work in db.scalars(sel).all():
        if not relink and work.id in linked_ids:
            continue
        scanned += 1
        try:
            if await match_and_enrich_work(db, work, provider):
                matched += 1
        except IntegrationError as exc:
            # An API-level failure (quota / block / 5xx): every other work would hit the same
            # wall, so abort the sweep and surface it rather than silently linking nothing.
            db.rollback()
            error = str(exc)
            log.warning("%s enrich aborted: %s", provider.kind, exc)
            break
        except Exception:  # noqa: BLE001 — one work failing shouldn't abort the sweep
            db.rollback()
            log.exception("enrich failed for work=%s", work.id)
        if scanned >= limit:
            break
        await asyncio.sleep(0.5)  # be polite to the provider (no documented rate limit)
    return {"scanned": scanned, "matched": matched, "provider": provider.kind, "error": error}


# ----------------------------------------------------------------- release watch
async def check_releases(db: Session, provider: MetadataProvider, *, limit: int = 60) -> dict:
    """For each linked work, re-fetch the provider and, if its release marker advanced (a new
    volume/chapter dropped), refresh metadata + trigger the title's own update so the app
    picks up the new release from its reading source."""
    from ..ingestion import tracker

    links = db.scalars(
        select(MetadataLink).where(MetadataLink.provider == provider.kind).limit(limit)
    ).all()
    checked = updated = 0
    error: str | None = None
    # Batch the by-id fetches up front (14B): a provider with a multi-id query (AniList id_in,
    # Hardcover _in) answers ~50 links per round-trip instead of one each — N → ~N/50 calls.
    metas: dict[str, "ProviderMeta | None"] = {}
    try:
        metas = await provider.fetch_many([link.ref for link in links if link.ref])
    except IntegrationError as exc:
        # API-level failure — abort the watch and surface it (a quota/block hits every link).
        log.warning("%s release-check aborted: %s", provider.kind, exc)
        return {"provider": provider.kind, "checked": 0, "updated": 0, "error": str(exc)}
    for link in links:
        checked += 1
        meta = metas.get(link.ref)
        if meta is None:
            continue
        changed = bool(meta.release_marker and meta.release_marker != link.release_marker)
        link.release_marker = meta.release_marker
        link.total_units = meta.total_units
        link.unit_kind = meta.unit_kind  # keep the unit current (a provider could change kinds)
        link.payload = {**(link.payload or {}),
                        "related": [{"title": r.title, "relation": r.relation, "ref": r.ref}
                                    for r in meta.related],
                        "status": meta.status}
        link.last_checked_at = datetime.now(UTC)
        work = db.get(Work, link.work_id)
        # Only act when the provider's marker actually ADVANCED (new volume/chapter upstream, or a
        # status flip) — re-acting on an unchanged marker would re-crawl a permanently-behind work
        # (source can't reach the provider's count) on every sweep. The first-time catch-up for an
        # already-behind link happens at link creation (match_and_enrich_work), not here.
        if work is not None and changed:
            cover_local = await _resolve_cover(meta)
            enrich_work(db, work, meta, cover_local)
            db.commit()
            # Chapter providers are the source of truth for the count: if they now list more than
            # we hold, raise the target + pull the missing ones. Volume/page providers can't drive
            # chapters, so a changed marker there just nudges the source for the new release.
            if meta.unit_kind == "chapters":
                if await reconcile_chapter_count(db, work, meta):
                    updated += 1
            else:
                try:  # nudge the work's own source to discover + enqueue the new release
                    await tracker.check_work(db, work)
                    updated += 1  # only count releases actually propagated to the reader
                except Exception:  # noqa: BLE001
                    db.rollback()
            # Be polite only when a change actually triggered downstream SOURCE traffic (reconcile /
            # tracker hit the work's own site). The provider fetch itself is already batched, so an
            # unchanged link needs no per-link delay.
            await asyncio.sleep(0.4)
        db.commit()
    return {"provider": provider.kind, "checked": checked, "updated": updated, "error": error}


async def sync_metadata_integration(db: Session, integ) -> dict:
    """Run a metadata integration's full sync and record its outcome on the integration row
    (``last_sync_at`` + ``last_error``). Shared by the manual 'Sync now' route and the 6-hourly
    scheduler so BOTH surface a provider API failure (e.g. Google Books quota) instead of
    silently linking nothing. Returns the run summary (with any ``error``)."""
    from .metadata import is_metadata_kind, provider_for

    if not is_metadata_kind(integ.kind):
        raise ValueError(f"{integ.kind!r} is not a metadata provider")
    error: str | None = None
    if integ.kind == "goodreads":  # wishlist import (no search API) → queued auto-hooks
        try:
            summary = await import_goodreads(db, integ)
        except IntegrationError as exc:  # a shelf/network failure → record it, don't 500
            summary, error = {"wanted": 0, "queued": 0}, str(exc)
    else:  # ranobedb / googlebooks — match + enrich hooked works, then watch for releases
        provider = provider_for(integ)
        summary = await enrich_library(db, provider)
        error = summary.get("error")
        if provider.tracks_releases and not error:  # don't pile a watch on a down API
            summary["releases"] = rel = await check_releases(db, provider)
            error = rel.get("error")
    integ.last_sync_at = datetime.now(UTC)
    integ.last_error = error
    db.commit()
    return summary


async def enrich_work_all_providers(db: Session, work: Work) -> None:
    """Best-effort: match + enrich ONE freshly-hooked work against every enabled search-capable
    metadata provider, so it shows authoritative metadata (and an expected count) immediately
    instead of waiting for the next 6-hourly sweep. Never raises — hooking must not depend on it.
    Goodreads is skipped (it's a wishlist source with no per-title search)."""
    from ..models import Integration
    from . import metadata as meta_mod

    # Run specialists FIRST so they claim the now-fill-when-empty display fields (MERGE-1): with
    # scan-id order a broad provider (Google Books) could run last and, before the enrich_work
    # upgrade-not-clobber fix, clobber RanobeDB/AniList author+cover. Lower number = earlier.
    _SPECIFICITY = {"ranobedb": 0, "anilist": 1, "hardcover": 2, "googlebooks": 3}
    integs = sorted(
        db.scalars(select(Integration).where(Integration.enabled.is_(True))).all(),
        key=lambda i: _SPECIFICITY.get(i.kind, 99),
    )
    for integ in integs:
        if not meta_mod.is_metadata_kind(integ.kind) or integ.kind == "goodreads":
            continue
        provider = meta_mod.provider_for(integ)
        # Skip headless-render providers (e.g. NovelUpdates without a cf_clearance cookie) on the
        # hot hook path — a slow browser render per hook would stall bulk auto-hooking. The
        # periodic sweep still enriches them. ``trigger_fetch=False``: hook_work just discovered
        # the source, so don't immediately re-run discovery for a chapter gap.
        if getattr(provider, "renders", False):
            continue
        try:
            await match_and_enrich_work(db, work, provider, trigger_fetch=False)
        except IntegrationError as exc:
            log.info("on-hook enrich: %s API unavailable for work=%s: %s",
                     integ.kind, work.id, exc)
        except Exception:  # noqa: BLE001 — enrichment must never break hooking
            db.rollback()
            log.exception("on-hook enrich failed work=%s provider=%s", work.id, integ.kind)


def _backfill_providers(db: Session) -> list[MetadataProvider]:
    """Providers the library-metadata backfill uses. The keyless trio (RanobeDB/AniList specialists,
    then Google Books for mainstream prose: rating/year/publisher/pages/isbn) works with no operator
    setup; any enabled metadata Integration (e.g. a Hardcover token) is added too, deduped by kind.
    Specialists first so they claim the upgrade-not-clobber display fields (MERGE-1)."""
    from .metadata import (AniListProvider, GoogleBooksProvider, RanobeDbProvider,
                           is_metadata_kind, provider_for)
    from ..models import Integration
    provs: dict[str, MetadataProvider] = {}
    for p in (RanobeDbProvider(), AniListProvider(), GoogleBooksProvider()):
        provs[p.kind] = p
    for integ in db.scalars(select(Integration).where(Integration.enabled.is_(True))).all():
        if is_metadata_kind(integ.kind) and integ.kind != "goodreads":
            provs.setdefault(integ.kind, provider_for(integ))
    return list(provs.values())


async def _enrich_work_meta(db: Session, work: Work,
                            provs: list[MetadataProvider]) -> bool:
    """Match + enrich ONE work against each provider (skipping headless-render ones). Never raises.
    Returns True if any provider was *transiently* unavailable (API down / rate-limited): the caller
    must then NOT stamp the work a definitive miss, so an outage doesn't poison it (mirrors the
    catalog enrich tick's transient handling). Shared by the backfill tick + the manual refresh."""
    transient = False
    for provider in provs:
        if getattr(provider, "renders", False):
            continue
        try:
            await match_and_enrich_work(db, work, provider, trigger_fetch=False)
        except IntegrationError as exc:
            transient = True   # provider down, not a real miss → leave the work due for retry
            log.info("metadata enrich: %s unavailable for work=%s: %s",
                     provider.kind, work.id, exc)
        except Exception:  # noqa: BLE001 — one bad provider/row must not abort the sweep
            db.rollback()
    return transient


async def enrich_one_work(db: Session, work: Work) -> None:
    """On-demand metadata refresh for a single work (the detail modal's 'Refresh metadata' action).
    Uses the same keyless+configured provider set as the backfill tick, then stamps it enriched."""
    transient = await _enrich_work_meta(db, work, _backfill_providers(db))
    if work.meta_enriched_at is None and not transient:
        work.meta_enriched_at = datetime.now(UTC)
        work.meta_source = work.meta_source or "none"
    db.commit()


async def backfill_work_metadata(db: Session, *, limit: int = 20) -> dict:
    """Fill the detail-modal display columns (rating/year/genres/publisher/pages/identifiers) on
    hooked works that were never enriched (``meta_enriched_at`` IS NULL), newest first. Bounded +
    polite, mirroring ``catalog_enrichment.enrich_catalog_tick``: a work that no provider matches is
    still stamped enriched so it stops being swept every tick (it just shows what it has)."""
    works = db.scalars(
        select(Work).where(Work.hooked.is_(True), Work.meta_enriched_at.is_(None))
        .order_by(Work.created_at.desc()).limit(max(1, limit))
    ).all()
    if not works:
        return {"scanned": 0, "enriched": 0}
    provs = _backfill_providers(db)
    scanned = enriched = 0
    for work in works:
        scanned += 1
        transient = await _enrich_work_meta(db, work, provs)
        if work.meta_enriched_at is not None:
            enriched += 1
        elif not transient:                 # definitive miss → stamp so it isn't re-swept forever
            work.meta_enriched_at = datetime.now(UTC)
            work.meta_source = work.meta_source or "none"
        # else: provider outage — leave NULL so the next tick retries (don't poison the work)
        db.commit()
        await asyncio.sleep(0.3)
    log.info("metadata backfill: scanned=%s enriched=%s", scanned, enriched)
    return {"scanned": scanned, "enriched": enriched}


# ----------------------------------------------------------------- related + queue
def _already_satisfied(db: Session, norm_key: str) -> bool:
    """True if a work with this normalized title is already hooked, or already queued/hooked."""
    if not norm_key:
        return True
    if db.scalar(
        select(QueuedHook.id).where(
            QueuedHook.norm_key == norm_key, QueuedHook.status.in_(["pending", "hooked"])
        )
    ):
        return True
    # Already a catalog entry hooked into the library with this title?
    if db.scalar(
        select(CatalogWork.id).where(
            CatalogWork.norm_key == norm_key, CatalogWork.hooked_work_id.is_not(None)
        )
    ):
        return True
    return False


def _hooked_work_id(db: Session, norm_key: str) -> int | None:
    """The Work id an already-hooked catalog entry for this title resolves to, if any."""
    if not norm_key:
        return None
    return db.scalar(
        select(CatalogWork.hooked_work_id).where(
            CatalogWork.norm_key == norm_key, CatalogWork.hooked_work_id.is_not(None)
        ).limit(1)
    )


def _want_title_for_user(db: Session, *, owner_id: int, target_shelf_id: int | None,
                         **qh_kwargs) -> str:
    """Make ``owner_id`` 'want' a title (per-user Goodreads/related). If the title is already
    hooked into a shared Work, add the user as a member now (membership only — the crawl is
    shared, no new job); otherwise queue an auto-hook stamped to this user. Deduped per user.
    Returns ``'member'`` | ``'queued'`` | ``'skip'``."""
    from ..library import add_to_library, in_library

    nk = qh_kwargs.get("norm_key") or ""
    wid = _hooked_work_id(db, nk)
    if wid is not None:
        if not in_library(db, owner_id, wid):
            add_to_library(db, owner_id, wid, shelf_id=target_shelf_id)
            return "member"
        return "skip"
    already = db.scalar(
        select(QueuedHook.id).where(
            QueuedHook.norm_key == nk, QueuedHook.user_id == owner_id,
            QueuedHook.status.in_(["pending", "hooked"]),
        )
    )
    if already:
        return "skip"
    db.add(QueuedHook(user_id=owner_id, target_shelf_id=target_shelf_id, **qh_kwargs))
    return "queued"


def _goodreads_target_shelf_id(db: Session, user_id: int | None) -> int | None:
    """The user's shelf marked as the Goodreads/auto-hook destination, if any."""
    if user_id is None:
        return None
    return db.scalar(
        select(Bookshelf.id)
        .where(Bookshelf.user_id == user_id, Bookshelf.goodreads_target.is_(True))
        .order_by(Bookshelf.sort_order, Bookshelf.id)
        .limit(1)
    )


def _primary_owner(db: Session, work_id: int) -> int | None:
    """The earliest library member of a work — used to attribute its related-title auto-hooks
    so a discovered sequel lands in that user's library rather than always the operator's."""
    return db.scalar(
        select(LibraryItem.user_id)
        .where(LibraryItem.work_id == work_id)
        .order_by(LibraryItem.added_at, LibraryItem.id)
        .limit(1)
    )


def queue_related(db: Session, work: Work, link: MetadataLink) -> int:
    """Queue every related title (prequel/sequel/side-story/…) from a work's metadata link
    for auto-hooking once it appears in the index. Returns how many were newly queued.

    The queued hooks are attributed to the source work's primary owner so the discovered title
    lands in their library (their goodreads_target shelf if they set one), not the operator's."""
    related = (link.payload or {}).get("related", []) or []
    owner_id = _primary_owner(db, work.id)
    target_shelf_id = _goodreads_target_shelf_id(db, owner_id)
    added = 0
    seen: set[str] = set()  # dedup within this call (session autoflush is off)
    for r in related:
        title = (r.get("title") or "").strip()
        nk = norm_title(title)
        if not title or nk in seen:
            continue
        seen.add(nk)
        qh_kwargs = dict(
            title=title[:512], norm_key=nk, author=work.author, media_kind=work.media_kind,
            reason="related", source=link.provider, relation=(r.get("relation") or "related"),
            related_work_id=work.id,
        )
        if owner_id is not None:
            if _want_title_for_user(
                db, owner_id=owner_id, target_shelf_id=target_shelf_id, **qh_kwargs
            ) == "queued":
                added += 1
        elif not _already_satisfied(db, nk):  # legacy/operator-owned related sync
            db.add(QueuedHook(**qh_kwargs))
            added += 1
    db.commit()
    return added


def _goodreads_shelf_targets(db: Session, integration, owner_id: int | None
                             ) -> list[tuple[str, int | None]]:
    """Which Goodreads shelves to pull and where each lands → list of (shelf_name, bookshelf_id).

    Always includes the connection's default shelf → the owner's goodreads_target bookshelf.
    Plus every bookshelf that named its own external Goodreads shelf (``goodreads_shelf``) →
    that bookshelf. A per-bookshelf mapping wins over the default for the same shelf name."""
    default_shelf = ((getattr(integration, "config", None) or {}).get("shelf")
                     or "to-read").strip() or "to-read"
    targets: dict[str, int | None] = {default_shelf: _goodreads_target_shelf_id(db, owner_id)}
    if owner_id is not None:
        for sid, gshelf in db.execute(
            select(Bookshelf.id, Bookshelf.goodreads_shelf).where(
                Bookshelf.user_id == owner_id, Bookshelf.goodreads_shelf.is_not(None)
            )
        ).all():
            name = (gshelf or "").strip()
            if name:
                targets[name] = sid
    return list(targets.items())


async def import_goodreads(db: Session, integration) -> dict:
    """Pull the owner's Goodreads shelves and queue each wanted book for auto-hooking once it's
    found in the index. Each shelf's titles land on its destination bookshelf (the connection's
    default shelf → the goodreads_target shelf; any bookshelf-named shelf → that bookshelf)."""
    from .metadata import provider_for

    # Per-user Goodreads: attribute the wishlist to the connection's owner so its auto-hooks land
    # in that user's library + the right bookshelf (NULL owner → first admin at delivery time).
    owner_id = getattr(integration, "user_id", None)
    targets = _goodreads_shelf_targets(db, integration, owner_id)
    wanted_total = queued = 0
    seen: set[str] = set()  # dedup across all shelves in this call (session autoflush is off)
    for shelf_name, target_shelf_id in targets:
        cfg = {**(getattr(integration, "config", None) or {}), "shelf": shelf_name}
        try:
            wanted = await provider_for(integration, cfg).wanted()
        except Exception as exc:  # noqa: BLE001 — one bad shelf shouldn't abort the others
            log.info("goodreads shelf %r failed: %s", shelf_name, exc)
            continue
        wanted_total += len(wanted)
        for w in wanted:
            nk = norm_title(w.title)
            if not nk or nk in seen:
                continue
            seen.add(nk)
            # media_kind is display-only here (Goodreads RSS doesn't distinguish prose vs manga);
            # matching/hooking is purely by norm_key, so a manga wishlist item still hooks correctly.
            qh_kwargs = dict(title=w.title[:512], norm_key=nk, author=w.author, media_kind="text",
                             reason="goodreads", source="goodreads")
            if owner_id is not None:
                # already-hooked title → membership only; else queue for this user + bookshelf.
                if _want_title_for_user(
                    db, owner_id=owner_id, target_shelf_id=target_shelf_id, **qh_kwargs
                ) == "queued":
                    queued += 1
            elif not _already_satisfied(db, nk):  # legacy/operator-owned connection
                db.add(QueuedHook(user_id=None, target_shelf_id=None, **qh_kwargs))
                queued += 1
    db.commit()
    return {"wanted": wanted_total, "queued": queued}


def _deliver_auto_hook(db: Session, qh, work_id: int) -> tuple[int, str] | None:
    """Add an auto-hooked work to its destination library/bookshelf. Uses the queued hook's owner
    (per-user Goodreads/related) + target shelf when set; otherwise the first admin so the title is
    never orphaned (member-less = invisible to everyone).

    Returns ``(user_id, title)`` for a ``library.added`` notification — the async caller dispatches
    it off the event loop (channel routing + per-event opt-in handled by the notifications engine)."""
    from ..library import add_to_library
    from ..models import User

    uid = getattr(qh, "user_id", None)
    if uid is None:
        admin = db.scalar(
            select(User).where(User.role == "admin", User.is_active.is_(True)).order_by(User.id)
        )
        uid = admin.id if admin else None
    if uid is None:
        return None
    shelf_id = getattr(qh, "target_shelf_id", None)
    add_to_library(db, uid, work_id, shelf_id=shelf_id)
    work = db.get(Work, work_id)
    return (uid, work.title if work else (qh.title or "A title"))


def _dljob_id(detail: str | None) -> int | None:
    if detail and detail.startswith("dljob:"):
        try:
            return int(detail.split(":", 1)[1])
        except ValueError:
            return None
    return None


async def _pipeline_fetch_queued(db: Session, qh) -> object | None:
    """Auto-fetch a queued title through the usenet pipeline when no crawlable source carries it.
    Resolves a book catalog row for the title (live if needed), then grabs the best confident
    release into the queued hook's owner/shelf. Returns the DownloadJob, or None."""
    from ..ingestion import book_catalog, downloads
    from ..models import Integration
    have_pipe = (
        db.scalar(select(Integration).where(Integration.kind == "prowlarr", Integration.enabled.is_(True)))
        and db.scalar(select(Integration).where(Integration.kind == "sabnzbd", Integration.enabled.is_(True)))
    )
    if not have_pipe:
        return None
    def _pick():
        rows = db.scalars(
            select(CatalogWork).where(
                CatalogWork.norm_key == qh.norm_key, CatalogWork.hooked_work_id.is_(None)
            )
        ).all()
        if not rows:
            return None
        if qh.author:  # avoid a same-title wrong-author edition (e.g. a study guide)
            for r in rows:
                if authors_compatible(qh.author, r.author):
                    return r
            return None
        return rows[0]

    cw = _pick()
    if cw is None:
        try:
            await book_catalog.resolve_live(db, f"{qh.title} {qh.author or ''}".strip())
        except Exception:  # noqa: BLE001
            return None
        cw = _pick()
    if cw is None:
        return None
    # Resolve an owner so the imported book is never orphaned: the hook's user, else the first admin.
    owner = qh.user_id
    if owner is None:
        from ..models import User
        admin = db.scalar(
            select(User).where(User.role == "admin", User.is_active.is_(True)).order_by(User.id)
        )
        owner = admin.id if admin else None
    try:
        return await downloads.auto_grab(db, cw, user_id=owner, shelf_id=qh.target_shelf_id,
                                         variant=getattr(qh, "variant", None) or "ebook")
    except Exception as exc:  # noqa: BLE001
        log.info("pipeline auto-fetch failed for %r: %s", qh.title, exc)
        return None


def _reconcile_downloading_hooks(db: Session) -> None:
    """Flip queued hooks that are downloading via the pipeline to their terminal state once the
    DownloadJob finishes (the import already added it to the library)."""
    from ..models import DownloadJob
    for qh in db.scalars(select(QueuedHook).where(QueuedHook.status == "downloading")).all():
        jid = _dljob_id(qh.detail)
        job = db.get(DownloadJob, jid) if jid else None
        if job is not None and job.status == "imported":
            qh.status, qh.hooked_work_id, qh.detail = "hooked", job.work_id, None
        elif job is not None and job.status in ("queued", "downloading", "completed"):
            continue  # still in flight — leave it
        else:
            # The download failed, or its record was deleted. Charge an attempt and retry up to the
            # cap so a permanently-failing title can't re-grab forever (re-hammering SAB/Prowlarr).
            attempts = (qh.attempts or 0) + 1
            qh.attempts = attempts
            if attempts >= MAX_HOOK_ATTEMPTS:
                qh.status, qh.detail = "failed", "download failed after retries"
            else:
                qh.status, qh.detail = "pending", f"download failed — retry {attempts}"
        db.commit()


async def process_queued_hooks(db: Session, *, limit: int = 12) -> dict:
    """Try to hook pending queued titles: first a matching, not-yet-hooked web-crawl catalog entry
    (its source must be enabled); otherwise fall back to the usenet pipeline (download + import).
    This is how a related/wishlist title is picked up automatically once it's obtainable anywhere."""
    from ..ingestion import catalog
    from ..ingestion.engine import ComplianceError

    _reconcile_downloading_hooks(db)
    pend = db.scalars(
        select(QueuedHook).where(QueuedHook.status == "pending")
        .order_by(QueuedHook.created_at).limit(limit)
    ).all()
    hooked = 0
    notifications: list[tuple[str, str]] = []  # (apprise_url, message) → pushed after the loop
    for qh in pend:
        if qh.status != "pending":
            continue  # already satisfied by a same-title hook earlier in this pass
        # An audiobook want is a SEPARATE audio Work — never satisfied by hooking a web-crawl EBOOK,
        # so it always goes through the pipeline (audiobook search), skipping the web-hook branch.
        want_audio = (getattr(qh, "variant", None) or "ebook") == "audiobook"
        cand = None if want_audio else db.scalar(
            select(CatalogWork).where(
                CatalogWork.provider == "web_index",
                CatalogWork.hooked_work_id.is_(None),
                CatalogWork.norm_key == qh.norm_key,
            ).limit(1)
        )
        if cand is None:
            # No crawlable web source carries it — fall back to the usenet pipeline (download).
            job = await _pipeline_fetch_queued(db, qh)
            if job is not None:
                qh.status = "downloading"
                qh.detail = f"dljob:{job.id}"
                db.commit()
            continue  # either downloading now, or not obtainable yet — keep waiting
        try:
            work = await catalog.hook_entry(db, cand)
            qh.status = "hooked"
            qh.hooked_work_id = work.id
            qh.detail = None
            hooked += 1
            # Land the auto-hook in a user's library: the queued hook's owner + its target
            # bookshelf if set (per-user Goodreads/related), else the first admin so it's never
            # orphaned (invisible). Per-shelf notify/auto-kindle handled by the shelf automation.
            spec = _deliver_auto_hook(db, qh, work.id)
            if spec:
                notifications.append(spec)
            # Hooking sets the candidate's hooked_work_id, so any OTHER pending hook for the SAME
            # title (e.g. a different user's Goodreads + a related-sync) would no longer find an
            # unhooked candidate and would be stranded. Satisfy them all now → each user's library.
            for other in db.scalars(
                select(QueuedHook).where(
                    QueuedHook.norm_key == qh.norm_key,
                    QueuedHook.status == "pending",
                    QueuedHook.id != qh.id,
                    # Same FORMAT only: a hooked ebook must not satisfy an audiobook want (which is a
                    # separate audio Work fetched via the pipeline), or it'd never be fetched.
                    QueuedHook.variant == (getattr(qh, "variant", None) or "ebook"),
                )
            ).all():
                other.status = "hooked"
                other.hooked_work_id = work.id
                other.detail = None
                spec = _deliver_auto_hook(db, other, work.id)
                if spec:
                    notifications.append(spec)
        except ComplianceError as exc:
            # Not a failure — the matching source just isn't enabled yet. Stay pending so it
            # hooks automatically once the operator enables the source (no attempt charged).
            qh.detail = f"source not enabled: {exc}"
        except Exception as exc:  # noqa: BLE001
            # A genuine hook failure. Charge an attempt; after MAX_HOOK_ATTEMPTS give up so a
            # permanently-broken candidate can't re-hammer the source or starve newer items.
            attempts = (qh.attempts or 0) + 1
            qh.attempts = attempts
            qh.detail = f"attempt {attempts}: {exc}"[:500]
            if attempts >= MAX_HOOK_ATTEMPTS:
                qh.status = "failed"
        db.commit()
    # Fire library.added notifications off the event loop (channel sends do blocking network I/O;
    # dispatch is defensive, so a failed push can't disturb the hook pipeline).
    if notifications:
        from .. import notifications as notif
        for uid, title in notifications:
            await asyncio.to_thread(
                notif.dispatch_threadsafe, "library.added",
                user_id=uid, title="Added to your library", body=title)
    return {"processed": len(pend), "hooked": hooked, "notified": len(notifications)}
