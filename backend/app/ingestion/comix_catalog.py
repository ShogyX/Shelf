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
import json
import logging
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from .. import telemetry
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import CatalogWork, IndexSite
from . import blocklist
from .extract import norm_title
from .. import config_store

log = logging.getLogger("shelf.indexer")

_SITE = "https://comix.to"
# Hit the canonical host directly — api.comix.to 301-redirects every call here, so pointing at
# comix.to/api/v1 skips a wasted round-trip per page.
_API = "https://comix.to/api/v1/manga"
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


async def _api_get(url: str) -> httpx.Response | None:
    """Plain JSON GET, replaying any cached Cloudflare clearance (cf_clearance cookie + the pinned UA)
    so a challenge-gated API answers directly. None on transport error. Caller checks the status."""
    from . import flaresolverr
    headers = {"Accept": "application/json", "Origin": _SITE,
               "User-Agent": "Mozilla/5.0 (compatible; ShelfReader/0.1)"}
    cookies: dict[str, str] = {}
    cl = flaresolverr.clearance_for(url)
    if cl:
        headers["User-Agent"] = cl.user_agent or headers["User-Agent"]  # cf_clearance is UA-bound
        cookies = dict(cl.cookies)
    try:
        async with telemetry.instrument("crawl", timeout=25.0, follow_redirects=True) as c:
            return await c.get(url, headers=headers, cookies=cookies)
    except httpx.HTTPError:
        return None


async def _fetch_page(page: int) -> dict | None:
    """One API page → its ``result`` dict ({items, meta}), or None on any failure (retry later).

    comix.to fronts this API with Cloudflare. When a request is challenged we solve it ONCE via the
    configured FlareSolverr proxy (``SHELF_FLARESOLVERR_URL``) to earn a domain-wide cf_clearance,
    then replay that cookie + the solver's UA on the plain JSON GETs for every page until it expires.
    Without a solver (or if the solver can't pass the challenge) the page just fails and the site
    cools down — same as before."""
    from . import challenge, flaresolverr
    from .netguard import BlockedAddress, assert_public_url

    url = f"{_API}?page={page}&limit={_PAGE_LIMIT}"
    try:
        await asyncio.to_thread(assert_public_url, url)  # SSRF guard (off the loop)
    except BlockedAddress:
        return None

    r = await _api_get(url)
    challenged = r is not None and challenge.is_challenge(r.status_code, r.headers, r.text)
    if (r is None or challenged) and flaresolverr.configured():
        # First failure may be an expired/absent clearance — solve the site root and retry once.
        if challenged:
            flaresolverr.invalidate(url)
        if await flaresolverr.ensure_clearance(url) is not None:
            r = await _api_get(url)

    if r is None or r.status_code != 200:
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
    # Prefer a freshly-localized DURABLE /covers/ cover (heals evicted/broken art), else the raw CDN
    # poster. A durable cover always wins; a remote URL is only adopted when we don't already have one.
    poster = item.get("poster") if isinstance(item.get("poster"), dict) else {}
    cover = (item.get("_cover") or "").strip() or (poster or {}).get("large") or (poster or {}).get("medium")
    if cover:
        cur = entry.cover_url or ""
        if cover.startswith("/covers/") or not cur.startswith("/covers/"):
            entry.cover_url = cover
    syn = (item.get("synopsis") or "").strip()
    if syn and len(syn) > len(entry.synopsis or ""):
        entry.synopsis = syn
    if item.get("originalLanguage"):
        entry.language = item["originalLanguage"]
    latest = item.get("latestChapter")
    if isinstance(latest, (int, float)) and latest > 0:
        entry.chapters_advertised = max(entry.chapters_advertised or 0, int(latest))
    # Popularity/rating come FREE in the list payload — capture them so the Index page can rank
    # comics by popularity (and enrich genres popular-first) without any extra calls.
    follows = item.get("followsTotal")
    if isinstance(follows, (int, float)) and follows >= 0:
        entry.popularity = float(follows)
    rated = item.get("ratedAvg")
    if isinstance(rated, (int, float)) and rated > 0:
        entry.rating = float(rated)
    rcount = item.get("ratedCount")
    if isinstance(rcount, (int, float)) and rcount >= 0:
        entry.rating_count = int(rcount)
    yr = item.get("year")
    if isinstance(yr, int) and yr > 0:
        entry.year = yr
    # Upgrade extra without blanking keys a prior pass set when this item omits them.
    extra = dict(entry.extra or {})
    extra["comix_type"] = (item.get("type") or "").lower()
    extra["comix_source"] = "browse"
    if item.get("year") is not None:
        extra["year"] = item["year"]
    if item.get("hid"):
        extra["hid"] = item["hid"]
    entry.extra = extra
    entry.updated_at = _utcnow()
    return created


# ------------------------------------------------------------ browser crawl (Cloudflare + token API)
# Only one comix browser crawl runs at a time (it owns a headful Chrome); overlapping ticks queue.
_browser_lock = asyncio.Lock()


async def _browser_crawl(start_page: int, count: int) -> dict | None:
    """Page the comix ``/browse`` grid through a real browser, in its OWN process.

    comix.to can't be read over plain HTTP any more — a Cloudflare Turnstile challenge fronts the
    site and the ``/api/v1/manga`` JSON API requires a per-request signed token. ``comix_browser``
    runs zendriver (which passes the challenge) under Xvfb and scrapes the server-rendered grid;
    running it as a subprocess keeps the heavy headful browser out of the app's event loop. Returns
    ``{"cards": [...], "pages": N, "ended": bool}`` or None on any failure (caller cools down)."""
    s = get_settings()
    env = dict(os.environ)
    cp = (config_store.effective("solver_chrome_path") or "").strip()
    if cp:
        env["SHELF_SOLVER_CHROME_PATH"] = cp
    # Headful Chrome needs an X display → wrap in xvfb-run. Budget: Chrome cold-start + the Cloudflare
    # solve + ~ a few seconds per page.
    cmd = ["xvfb-run", "-a", "-s", "-screen 0 1280x1024x24",
           sys.executable, "-m", "app.ingestion.comix_browser", str(start_page), str(count)]
    # Budget per page: ~4s nav + up to 28 CDN cover fetches via curl_cffi; plus the one-time CF solve.
    timeout = 120 + count * 35
    repo_root = str(Path(__file__).resolve().parents[2])
    try:
        async with _browser_lock:
            proc = await asyncio.create_subprocess_exec(
                *cmd, cwd=repo_root, env=env,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            try:
                out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except asyncio.TimeoutError:
                proc.kill()
                log.warning("comix browser crawl timed out after %ss (pages %s..%s)",
                            timeout, start_page, start_page + count - 1)
                return None
    except (FileNotFoundError, OSError) as exc:
        log.warning("comix browser crawl unavailable (%s) — is xvfb-run installed?", exc)
        return None
    if proc.returncode != 0:
        tail = (err or b"")[-300:].decode("utf-8", "replace")
        log.warning("comix browser crawl exited %s: %s", proc.returncode, tail)
        return None
    try:
        data = json.loads((out or b"").decode("utf-8", "replace"))
    except ValueError:
        log.warning("comix browser crawl produced no JSON")
        return None
    return data if isinstance(data, dict) else None


def upsert_card(db: Session, site: IndexSite, card: dict) -> bool:
    """Create/update a CatalogWork from a browse-grid card ``{url, hid, slug, title, cover}``. Returns
    True when a NEW row was created. The de-slugged title seeds a new row; a later metadata enrichment
    upgrades it, so we never overwrite a richer title once set."""
    work_url = (card.get("url") or "").strip().rstrip("/")
    title = (card.get("title") or "").strip()
    if not work_url or not title or blocklist.is_blocked(db, work_url):
        return False
    entry = db.scalar(select(CatalogWork).where(
        CatalogWork.site_id == site.id, CatalogWork.work_url == work_url))
    created = entry is None
    if entry is None:
        entry = CatalogWork(site_id=site.id, work_url=work_url, domain=site.domain, title=title[:512])
        db.add(entry)
        entry.title = title[:512]
        entry.norm_key = norm_title(title)
    entry.media_kind = "comic"
    entry.kind = "work"
    cover = (card.get("cover") or "").strip()
    if cover:
        cur = entry.cover_url or ""
        # A freshly-localized DURABLE /covers/ cover always wins (heals an evicted/broken one); a raw
        # remote cover is only adopted when we don't already have a durable one.
        if cover.startswith("/covers/") or not cur.startswith("/covers/"):
            entry.cover_url = cover
    extra = dict(entry.extra or {})
    if card.get("hid"):
        extra["hid"] = card["hid"]
    extra["comix_source"] = "browse"
    entry.extra = extra
    entry.updated_at = _utcnow()
    return created


async def ingest_tick(db: Session, site: IndexSite, *, max_pages: int | None = None) -> dict:
    """Advance comix.to's catalog by crawling ``max_pages`` browse pages through the browser (zendriver
    past Cloudflare), upserting each title. Bounded per call; resumes from ``api_cursor`` next tick. A
    page that returns no cards means the catalog is exhausted → park (cursor 0) + stamp ``api_synced_at``
    so a refresh fires after ``_REFRESH_AFTER``. A crawl failure backs off without losing the cursor."""
    now = _utcnow()
    if not is_due(site, now):
        return {"created": 0, "scanned": 0, "done": True}
    s = get_settings()
    if not config_store.effective("comix_browser_enabled"):
        return {"created": 0, "scanned": 0, "done": True}
    count = max(1, max_pages if max_pages is not None else config_store.effective("comix_browser_pages_per_tick"))
    start = site.api_cursor or 1   # idle+due → fresh pass at page 1

    result = await _browser_crawl(start, count)
    if result is None:
        site.api_cursor = start                      # persist where to resume
        site.cooldown_until = now + _RETRY_BACKOFF    # crawl failed → back off, keep the cursor
        db.commit()
        log.info("comix catalog: site=%s browser crawl failed at page %s; cooling down", site.id, start)
        return {"created": 0, "scanned": 0, "cursor": site.api_cursor, "done": False}

    api_items = result.get("items") or []
    cards = result.get("cards") or []
    pages = int(result.get("pages") or 0)
    ended = bool(result.get("ended"))
    created = scanned = 0
    seen_urls: set[str] = set()
    # Rich API items first — full metadata (popularity/rating/year/synopsis) so titles rank instead of
    # stranding at popularity 0. Then any DOM-only fallback cards the API capture missed. Dedup by
    # work_url across the batch (the grid can repeat a title across pages → UNIQUE violation).
    for item in api_items:
        url = _work_url(item)
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        scanned += 1
        try:
            if upsert_item(db, site, item):
                created += 1
        except Exception:  # noqa: BLE001 — one bad item shouldn't abort the batch
            db.rollback()
    for card in cards:
        url = (card.get("url") or "").strip().rstrip("/")
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        scanned += 1
        try:
            if upsert_card(db, site, card):
                created += 1
        except Exception:  # noqa: BLE001 — one bad card shouldn't abort the batch
            db.rollback()
    db.commit()

    if ended:
        site.api_cursor = 0
        site.api_synced_at = now
    else:
        site.api_cursor = start + max(pages, 1)   # resume after the pages we crawled
    if created:
        site.titles_found = db.scalar(
            select(func.count(CatalogWork.id)).where(CatalogWork.site_id == site.id)
        ) or site.titles_found
        site.pages_since_new_title = 0   # productive → keep the site from idle-stopping
    db.commit()
    log.info("comix catalog: site=%s pages=%s cursor->%s created=%s scanned=%s ended=%s",
             site.id, pages, site.api_cursor, created, scanned, ended)
    return {"created": created, "scanned": scanned, "cursor": site.api_cursor, "done": ended}


async def _ingest_tick_api(db: Session, site: IndexSite, *, max_pages: int = _PAGES_PER_TICK) -> dict:
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
