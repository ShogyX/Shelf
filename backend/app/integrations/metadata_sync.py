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
from ..models import (
    Bookshelf,
    CatalogWork,
    LibraryItem,
    MetadataLink,
    QueuedHook,
    UserSettings,
    Work,
)
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


async def _best_for_query(provider: MetadataProvider, query: str, score_title: str,
                          author: str | None) -> tuple[float, ProviderMatch] | None:
    matches = await provider.search(query, author)
    scored = sorted(((_confidence(score_title, author, m), m) for m in matches),
                    key=lambda x: x[0], reverse=True)
    return scored[0] if scored else None


async def best_match(provider: MetadataProvider, title: str, author: str | None
                     ) -> tuple[float, ProviderMatch] | None:
    bm = await _best_for_query(provider, title, title, author)
    if bm is not None and bm[0] >= MATCH_THRESHOLD:
        return bm
    # First query missed — retry once with a cleaned title, scoring against that cleaned title
    # too (the noise we stripped, e.g. '… - Chapter 12 | Site', otherwise dilutes the score of
    # an otherwise-perfect candidate). The clean is conservative, so this widens recall without
    # inviting wrong matches.
    alt_q = _clean_query(title)
    if alt_q and alt_q.lower() != (title or "").lower():
        alt = await _best_for_query(provider, alt_q, alt_q, author)
        if alt is not None and (bm is None or alt[0] > bm[0]):
            return alt
    return bm


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


def _deliver_auto_hook(db: Session, qh, work_id: int) -> tuple[str, str] | None:
    """Add an auto-hooked work to its destination library/bookshelf. Uses the queued hook's owner
    (per-user Goodreads/related) + target shelf when set; otherwise the first admin so the title is
    never orphaned (member-less = invisible to everyone).

    Returns ``(apprise_url, message)`` when the destination shelf has ``notify_on_add`` and the
    owner has a push URL configured — the async caller dispatches the push off the event loop."""
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
    if shelf_id is None:
        return None
    shelf = db.get(Bookshelf, shelf_id)
    if shelf is None or shelf.user_id != uid or not shelf.notify_on_add:
        return None
    us = db.scalar(select(UserSettings).where(UserSettings.user_id == uid))
    url = (us.apprise_url if us else None) or ""
    if not url.strip():
        return None
    work = db.get(Work, work_id)
    title = work.title if work else (qh.title or "A title")
    return (url, f"Added to your “{shelf.name}” shelf: {title}")


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
    notifications: list[tuple[str, str]] = []  # (apprise_url, message) → pushed after the loop
    for qh in pend:
        if qh.status != "pending":
            continue  # already satisfied by a same-title hook earlier in this pass
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
    # Fire per-shelf push notifications off the event loop (apprise does blocking network I/O;
    # notify() never raises, so a failed push can't disturb the hook pipeline).
    if notifications:
        from ..notify import notify
        for url, msg in notifications:
            await asyncio.to_thread(notify, url, "Shelf", msg)
    return {"processed": len(pend), "hooked": hooked, "notified": len(notifications)}
