"""Ingest comix.to's catalog via its open JSON API instead of HTML-crawling the SPA.

comix.to is a JS single-page app whose ``/browse`` only renders a recently-updated *slice* of
its library (heavily manhwa), so the HTML crawler never reaches the bulk of the catalog —
mainstream manga like One Piece / Kingdom were simply never discovered. Its open API
(``api.comix.to/api/v1/manga?page=N``) lists the WHOLE catalog with full metadata, so we page
through it and upsert :class:`CatalogWork` rows directly (no per-title fetch).

Incremental + bounded: a few pages per crawl tick, with the next page tracked on
``IndexSite.api_cursor``; a completed full pass stamps ``api_synced_at`` and the site refreshes
periodically to pick up newly-added titles. Entries dedupe with any HTML-crawled ones because
the API's ``url`` (``/title/<hid>-<slug>``) matches the browse-scraped work URL exactly.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import CatalogWork, IndexSite
from . import blocklist
from .extract import norm_title

log = logging.getLogger("shelf.indexer")

_SITE = "https://comix.to"
_API = "https://api.comix.to/api/v1/manga"
_PAGE_LIMIT = 100          # API max per page
_PAGES_PER_TICK = 5        # bounded work per crawl pass (≈500 titles/tick)
_PAGE_PAUSE_S = 0.4        # politeness between API pages
_REFRESH_AFTER = timedelta(hours=12)  # re-page the catalog this often to catch new titles
_RETRY_BACKOFF = timedelta(minutes=5)  # cool the site this long after an API failure (no spin)
# Keep readable comics; skip 'other' (databooks / doujin / official-art / novels).
_KEEP_TYPES = {"manga", "manhwa", "manhua"}
_STATUS = {"completed": "complete", "finished": "complete", "cancelled": "complete",
           "ongoing": "ongoing", "on_hiatus": "ongoing", "hiatus": "ongoing",
           "releasing": "ongoing"}


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _aware(dt: datetime | None) -> datetime | None:
    return dt if (dt is None or dt.tzinfo) else dt.replace(tzinfo=UTC)


def is_api_catalog_site(site: IndexSite) -> bool:
    """True for sites whose catalog we page from a JSON API rather than HTML-crawl."""
    d = (site.domain or "").lower()
    if d.startswith("www."):
        d = d[4:]
    return d == "comix.to" or d.endswith(".comix.to")


def is_due(site: IndexSite, now: datetime | None = None) -> bool:
    """Whether the API catalog has pages left to fetch, or a refresh pass is due."""
    if (site.api_cursor or 0) > 0:
        return True  # mid-pass
    synced = _aware(site.api_synced_at)
    return synced is None or (now or _utcnow()) - synced >= _REFRESH_AFTER


async def _fetch_page(page: int) -> dict | None:
    """One API page → its ``result`` dict ({items, meta}), or None on any failure (retry later)."""
    from .netguard import BlockedAddress, assert_public_url

    url = f"{_API}?page={page}&limit={_PAGE_LIMIT}"
    try:
        await asyncio.to_thread(assert_public_url, url)  # SSRF guard (off the loop)
    except BlockedAddress:
        return None
    try:
        async with httpx.AsyncClient(timeout=25.0, follow_redirects=True) as c:
            r = await c.get(url, headers={
                "Accept": "application/json", "Origin": _SITE,
                "User-Agent": "Mozilla/5.0 (compatible; ShelfReader/0.1)"})
    except httpx.HTTPError:
        return None
    if r.status_code != 200:
        return None
    try:
        body = r.json()
    except Exception:  # noqa: BLE001
        return None
    return body.get("result") if isinstance(body, dict) else None


def _work_url(item: dict) -> str | None:
    path = (item.get("url") or "").strip()
    if not path:
        hid = item.get("hid")
        path = f"/title/{hid}" if hid else ""
    if not path:
        return None
    # Normalize to the exact browse-scraped form (no trailing slash) so an API entry dedupes
    # with any HTML-crawled row for the same series instead of creating a second card.
    return (path if path.startswith("http") else f"{_SITE}{path}").rstrip("/")


def upsert_item(db: Session, site: IndexSite, item: dict) -> bool:
    """Create/update a CatalogWork from one API item. Returns True if a NEW entry was created.
    Skips non-comic types and blocklisted URLs. Only upgrades richer fields, never blanks them."""
    if (item.get("type") or "").lower() not in _KEEP_TYPES:
        return False
    work_url = _work_url(item)
    title = (item.get("title") or "").strip()
    if not work_url or not title or blocklist.is_blocked(db, work_url):
        return False
    entry = db.scalar(select(CatalogWork).where(
        CatalogWork.site_id == site.id, CatalogWork.work_url == work_url))
    created = entry is None
    if entry is None:
        entry = CatalogWork(site_id=site.id, work_url=work_url, domain=site.domain,
                            title=title[:512])
        db.add(entry)
    entry.title = title[:512]
    entry.norm_key = norm_title(title)
    entry.media_kind = "comic"
    entry.kind = "work"
    poster = item.get("poster") if isinstance(item.get("poster"), dict) else {}
    cover = (poster or {}).get("large") or (poster or {}).get("medium")
    if cover:
        entry.cover_url = cover
    syn = (item.get("synopsis") or "").strip()
    if syn and len(syn) > len(entry.synopsis or ""):
        entry.synopsis = syn
    if item.get("originalLanguage"):
        entry.language = item["originalLanguage"]
    latest = item.get("latestChapter")
    if isinstance(latest, (int, float)) and latest > 0:
        entry.chapters_advertised = max(entry.chapters_advertised or 0, int(latest))
    # Upgrade extra without blanking keys a prior pass set when this item omits them.
    extra = dict(entry.extra or {})
    extra["comix_type"] = (item.get("type") or "").lower()
    if item.get("year") is not None:
        extra["year"] = item["year"]
    if item.get("hid"):
        extra["hid"] = item["hid"]
    entry.extra = extra
    entry.updated_at = _utcnow()
    return created


async def ingest_tick(db: Session, site: IndexSite, *, max_pages: int = _PAGES_PER_TICK) -> dict:
    """Advance comix.to's API catalog by up to ``max_pages`` pages, upserting each title as a
    CatalogWork. Bounded per call; resumes from ``site.api_cursor`` next tick. On finishing a full
    pass it parks (cursor 0) and stamps ``api_synced_at`` so a refresh fires after ``_REFRESH_AFTER``."""
    now = _utcnow()
    if not is_due(site, now):
        return {"created": 0, "scanned": 0, "done": True}
    page = site.api_cursor or 1  # idle+due → start a fresh pass at page 1
    created = scanned = 0
    last_page: int | None = None
    completed = failed = False
    for _ in range(max(1, max_pages)):
        result = await _fetch_page(page)
        if result is None:
            failed = True  # transient API failure — keep the cursor, back off, retry later
            break
        items = result.get("items") or []
        last_page = (result.get("meta") or {}).get("lastPage") or last_page
        if not items:
            # End of catalog only if we're genuinely past the last page; an empty page BEFORE the
            # end is an API hiccup — treat it as a failure (retry) so we never falsely mark synced
            # and drop the rest of the catalog.
            if last_page is not None and page > last_page:
                completed = True
            else:
                failed = True
            break
        for it in items:
            scanned += 1
            try:
                if upsert_item(db, site, it):
                    created += 1
            except Exception:  # noqa: BLE001 — one bad item shouldn't abort the page
                db.rollback()
        db.commit()
        page += 1
        if last_page is not None and page > last_page:  # ingested the last page → done
            completed = True
            break
        await asyncio.sleep(_PAGE_PAUSE_S)
    if completed:
        site.api_cursor = 0
        site.api_synced_at = now
    else:
        site.api_cursor = page  # resume here next tick (unchanged on failure)
        if failed:  # don't retry a dead/blocking API every tick — cool the site briefly
            site.cooldown_until = now + _RETRY_BACKOFF
    if created:
        site.titles_found = db.scalar(
            select(func.count(CatalogWork.id)).where(CatalogWork.site_id == site.id)
        ) or site.titles_found
        site.pages_since_new_title = 0  # productive → keep the site from idle-stopping
    db.commit()
    log.info("comix catalog: site=%s page->%s created=%s scanned=%s lastPage=%s done=%s",
             site.id, site.api_cursor, created, scanned, last_page, completed)
    return {"created": created, "scanned": scanned, "cursor": site.api_cursor,
            "last_page": last_page, "done": completed}
