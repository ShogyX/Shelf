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
from ..ingestion.extract import authors_compatible, norm_title
from ..models import CatalogWork, MetadataLink, QueuedHook, Work
from .metadata import MetadataProvider, ProviderMatch, ProviderMeta

log = logging.getLogger("shelf.metadata")

MATCH_THRESHOLD = 0.6  # below this we don't auto-link (avoid wrong source-of-truth)
MAX_HOOK_ATTEMPTS = 5  # give up auto-hooking a queued title after this many genuine failures


def _confidence(work_title: str, work_author: str | None, m: ProviderMatch) -> float:
    """How confidently a provider match is the same work — normalized title overlap, gated
    by author compatibility so a same-titled different work doesn't get linked."""
    a, b = norm_title(work_title), norm_title(m.title)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0 if authors_compatible(work_author, m.author) else 0.55
    ta, tb = set(a.split()), set(b.split())
    if len(ta) < 2 or len(tb) < 2:
        return 0.0
    jac = len(ta & tb) / len(ta | tb)
    return jac if authors_compatible(work_author, m.author) else jac * 0.7


async def best_match(provider: MetadataProvider, title: str, author: str | None
                     ) -> tuple[float, ProviderMatch] | None:
    matches = await provider.search(title, author)
    scored = sorted(((_confidence(title, author, m), m) for m in matches),
                    key=lambda x: x[0], reverse=True)
    return scored[0] if scored else None


def enrich_work(db: Session, work: Work, meta: ProviderMeta, cover_local: str | None = None) -> None:
    """Overwrite the work's displayed metadata from the provider (the source of truth).
    Only non-empty provider fields are applied. ``cover_local`` is a pre-resolved cached
    cover URL (caching is done off the event loop by the caller)."""
    if meta.author:
        work.author = meta.author[:255]
    if meta.synopsis:
        work.description = meta.synopsis
    if cover_local and cover_local != imagecache.PERMANENT_FAIL:
        work.cover_url = cover_local
    elif meta.cover_url and cover_local is None:
        work.cover_url = meta.cover_url  # caller didn't cache; use the remote URL
    # Only a CHAPTER-unit count may drive the crawl/completeness target, and never LOWER it.
    # A provider VOLUME count (ranobedb) is kept on the MetadataLink, not written here —
    # writing 33 volumes onto a 2000-chapter work would corrupt health + stop the backfill.
    if meta.total_units and meta.unit_kind == "chapters":
        work.total_chapters_expected = max(work.total_chapters_expected or 0, meta.total_units)
    if meta.media_kind == "comic" and work.media_kind != "comic":
        work.media_kind = "comic"


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


async def match_and_enrich_work(db: Session, work: Work, provider: MetadataProvider
                                ) -> MetadataLink | None:
    """Find + link the best provider match for one work and enrich it. Returns the link
    (or None if nothing matched confidently)."""
    bm = await best_match(provider, work.title, work.author)
    if bm is None or bm[0] < MATCH_THRESHOLD:
        return None
    confidence, match = bm
    meta = await provider.fetch(match.ref)
    if meta is None:
        return None
    # Cache the cover off the event loop (blocking DNS + download).
    cover_local = None
    if meta.cover_url:
        cover_local = await asyncio.to_thread(imagecache.cache_image, meta.cover_url)
    link = upsert_link(db, work, provider.kind, meta, confidence)
    enrich_work(db, work, meta, cover_local)
    db.commit()
    log.info("linked work=%s -> %s:%s (conf=%.2f)", work.id, provider.kind, meta.ref, confidence)
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
    for work in db.scalars(sel).all():
        if not relink and work.id in linked_ids:
            continue
        scanned += 1
        try:
            if await match_and_enrich_work(db, work, provider):
                matched += 1
        except Exception:  # noqa: BLE001 — one work failing shouldn't abort the sweep
            db.rollback()
            log.exception("enrich failed for work=%s", work.id)
        if scanned >= limit:
            break
        await asyncio.sleep(0.5)  # be polite to the provider (no documented rate limit)
    return {"scanned": scanned, "matched": matched, "provider": provider.kind}


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
    for link in links:
        checked += 1
        try:
            meta = await provider.fetch(link.ref)
        except Exception:  # noqa: BLE001
            continue
        if meta is None:
            continue
        changed = bool(meta.release_marker and meta.release_marker != link.release_marker)
        link.release_marker = meta.release_marker
        link.total_units = meta.total_units
        link.payload = {**(link.payload or {}),
                        "related": [{"title": r.title, "relation": r.relation, "ref": r.ref}
                                    for r in meta.related],
                        "status": meta.status}
        link.last_checked_at = datetime.now(UTC)
        if changed:
            work = db.get(Work, link.work_id)
            if work is not None:
                cover_local = None
                if meta.cover_url:
                    cover_local = await asyncio.to_thread(imagecache.cache_image, meta.cover_url)
                enrich_work(db, work, meta, cover_local)
                db.commit()
                try:  # nudge the work's own source to discover + enqueue the new release
                    await tracker.check_work(db, work)
                    updated += 1  # only count releases actually propagated to the reader
                except Exception:  # noqa: BLE001
                    db.rollback()
        db.commit()
        await asyncio.sleep(0.4)
    return {"provider": provider.kind, "checked": checked, "updated": updated}


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


def queue_related(db: Session, work: Work, link: MetadataLink) -> int:
    """Queue every related title (prequel/sequel/side-story/…) from a work's metadata link
    for auto-hooking once it appears in the index. Returns how many were newly queued."""
    related = (link.payload or {}).get("related", []) or []
    added = 0
    seen: set[str] = set()  # dedup within this call (session autoflush is off)
    for r in related:
        title = (r.get("title") or "").strip()
        nk = norm_title(title)
        if not title or nk in seen or _already_satisfied(db, nk):
            continue
        seen.add(nk)
        db.add(QueuedHook(
            title=title[:512], norm_key=nk, author=work.author, media_kind=work.media_kind,
            reason="related", source=link.provider, relation=(r.get("relation") or "related"),
            related_work_id=work.id,
        ))
        added += 1
    db.commit()
    return added


async def import_goodreads(db: Session, integration) -> dict:
    """Pull the user's Goodreads shelf and queue each wanted book for auto-hooking once it's
    found in the index."""
    from .metadata import provider_for

    provider = provider_for(integration)
    wanted = await provider.wanted()
    queued = 0
    seen: set[str] = set()  # dedup within this call (session autoflush is off)
    for w in wanted:
        nk = norm_title(w.title)
        if not nk or nk in seen or _already_satisfied(db, nk):
            continue
        seen.add(nk)
        db.add(QueuedHook(
            # media_kind is display-only here (Goodreads RSS doesn't distinguish prose vs
            # manga); matching/hooking is purely by norm_key, so a manga wishlist item still
            # hooks correctly against its catalog entry.
            title=w.title[:512], norm_key=nk, author=w.author, media_kind="text",
            reason="goodreads", source="goodreads",
        ))
        queued += 1
    db.commit()
    return {"wanted": len(wanted), "queued": queued}


async def process_queued_hooks(db: Session, *, limit: int = 12) -> dict:
    """Try to hook pending queued titles: find a matching, not-yet-hooked web-crawl catalog
    entry and hook it (its source must be enabled — else it stays pending). This is how a
    related/wishlist title is picked up automatically once it appears in the index."""
    from ..ingestion import catalog
    from ..ingestion.engine import ComplianceError

    pend = db.scalars(
        select(QueuedHook).where(QueuedHook.status == "pending")
        .order_by(QueuedHook.created_at).limit(limit)
    ).all()
    hooked = 0
    for qh in pend:
        cand = db.scalar(
            select(CatalogWork).where(
                CatalogWork.provider == "web_index",
                CatalogWork.hooked_work_id.is_(None),
                CatalogWork.norm_key == qh.norm_key,
            ).limit(1)
        )
        if cand is None:
            continue  # not in the index yet — keep waiting
        try:
            work = await catalog.hook_entry(db, cand)
            qh.status = "hooked"
            qh.hooked_work_id = work.id
            qh.detail = None
            hooked += 1
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
    return {"processed": len(pend), "hooked": hooked}
