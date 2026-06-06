"""URL indexer — polite auto-crawl of a chosen web location into a searchable store.

A user submits a root URL; ``start_index`` creates an IndexSite + a pending root
page. The scheduler's ``index_tick`` drains pending pages slowly within the
web_index source's rate budget, extracting readable text + same-host links and
enqueueing newly-discovered pages up to the site's page/depth bounds. Every
fetched page is mirrored into the FTS5 index for ranked, snippet-able search.
"""
from __future__ import annotations

import asyncio
import logging
import warnings
from datetime import UTC, datetime, timedelta
from urllib.parse import urldefrag, urljoin, urlparse

from bs4 import BeautifulSoup

try:  # sitemaps/feeds encountered while crawling parse as XML; silence the noise.
    from bs4 import XMLParsedAsHTMLWarning

    warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
except Exception:  # pragma: no cover
    pass
from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import SessionLocal, index_fts_upsert
from ..models import CatalogWork, IndexedPage, IndexSite, Source
from ..sanitize import count_words, sanitize_html
from . import catalog
from .engine import ComplianceError, ensure_source, get_fetcher
from .extract import (
    extract_main_content,
    is_chapter_url,
    is_junk_url,
    is_noncatalog_content_url,
    is_work_url,
    link_priority,
    og_title,
    page_metadata,
    work_url_for,
)

log = logging.getLogger("shelf.indexer")
settings = get_settings()
SOURCE_KEY = "web_index"

# Don't enqueue obvious non-document assets as pages.
_SKIP_EXTS = (
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico", ".css", ".js",
    ".pdf", ".zip", ".gz", ".tar", ".7z", ".iso", ".mp3", ".mp4", ".mov", ".avi",
    ".woff", ".woff2", ".ttf", ".xml", ".json", ".rss", ".atom",
    ".txt", ".mxl", ".mid", ".midi",  # Gutenberg ships book text + sheet-music assets here
)

# Download/format tokens that mark a URL as a FILE, not a crawlable page — even when they aren't
# the final extension. Project Gutenberg's download links look like '/ebooks/35.epub.images',
# '/ebooks/35.kindle.images', '/ebooks/35.kf8.images' (path ends in '.images', so an extension
# check alone misses them). Match any of these tokens appearing as a dot-segment of the last path
# component so the crawler never navigates to them (which makes Playwright throw "Download is
# starting" and — before this — cooled the whole site down).
_DOWNLOAD_TOKENS = frozenset({
    "epub", "epub3", "mobi", "kindle", "kf8", "azw", "azw3", "txt", "rtf",
    "mxl", "rdf", "tei", "opf", "qioo",
})


def _is_asset_url(url: str) -> bool:
    """True for URLs that are downloadable files / non-document assets, not crawlable HTML pages."""
    path = urlparse(url).path.lower()
    if path.endswith(_SKIP_EXTS):
        return True
    last = path.rsplit("/", 1)[-1]
    if "." in last:
        # tokens after the name, e.g. '35.epub.images' -> {'epub', 'images'}
        if set(last.split(".")[1:]) & _DOWNLOAD_TOKENS:
            return True
    return False


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _norm(url: str) -> str:
    return urldefrag(url)[0].strip()


def _domain(url: str) -> str:
    return (urlparse(url).netloc or "").lower()


_IDLE_KEY = "index_stop_after_idle_pages"


def global_idle_default(db: Session) -> int:
    """The operator-set global idle-stop default (Settings → Indexing), or the config
    fallback. Used when a new site is created."""
    from ..models import AppSetting

    row = db.get(AppSetting, _IDLE_KEY)
    if row and isinstance(row.value, dict) and isinstance(row.value.get("value"), int):
        return max(1, row.value["value"])
    return settings.index_stop_after_idle_pages


def set_global_idle_default(db: Session, n: int) -> int:
    from ..models import AppSetting

    n = max(1, int(n))
    row = db.get(AppSetting, _IDLE_KEY)
    if row is None:
        row = AppSetting(key=_IDLE_KEY, value={"value": n})
        db.add(row)
    else:
        row.value = {"value": n}
    db.commit()
    return n


def _web_index_source(db: Session) -> Source:
    from .base import registry

    src = ensure_source(db, registry.get(SOURCE_KEY))
    if not src.tos_permitted:
        raise ComplianceError(
            "The Web index source is disabled. Enable it on the Sources page to index URLs."
        )
    return src


def start_index(
    db: Session,
    url: str,
    *,
    max_pages: int | None = None,
    max_depth: int | None = None,
    same_host_only: bool = True,
    update_indexed: bool = False,
) -> IndexSite:
    """Create a site + seed its root page (pending). Raises ComplianceError if disabled.

    Re-adding a URL that was indexed before (including one that was *removed* — soft-deleted)
    reuses the existing site and resumes from where it left off. By design the crawl does NOT
    re-fetch pages it already indexed: the dedup in ``_enqueue_links`` plus the pending frontier
    mean only new/unfinished pages get crawled, so remove-then-re-add never repeats prior work.
    Pass ``update_indexed=True`` to explicitly refresh: every already-processed page is re-queued
    so the crawl re-fetches it and picks up changes.
    """
    url = _norm(url)
    if not urlparse(url).scheme:
        url = "https://" + url
        url = _norm(url)
    _web_index_source(db)  # compliance gate

    site = db.scalar(select(IndexSite).where(IndexSite.root_url == url))
    if site is None:
        site = IndexSite(
            root_url=url,
            domain=_domain(url),
            max_pages=max_pages or settings.index_max_pages,
            max_depth=max_depth if max_depth is not None else settings.index_max_depth,
            same_host_only=same_host_only,
            stop_after_idle_pages=global_idle_default(db),
            status="active",
        )
        db.add(site)
        db.commit()
        db.refresh(site)
    else:
        # Re-adding an existing site (finished / paused / removed): reactivate it, reset the idle
        # counter so it can resume discovering, and clear any leftover backoff so it starts at
        # full speed. Pages already fetched are left untouched (no repeat) unless update_indexed.
        site.status = "active"
        site.pages_since_new_title = 0
        site.consecutive_errors = 0
        site.cooldown_until = None
        if not site.stop_after_idle_pages:
            site.stop_after_idle_pages = global_idle_default(db)
        if max_pages:
            site.max_pages = max_pages
        if max_depth is not None:
            site.max_depth = max_depth
        if update_indexed:
            # Explicit opt-in: re-queue every previously-processed page (fetched/failed/skipped)
            # back into the frontier so the crawl re-fetches it and refreshes its content. Their
            # existing rows (and FTS entries) stay in place until re-fetched, so search keeps
            # working during the refresh.
            db.execute(
                update(IndexedPage)
                .where(
                    IndexedPage.site_id == site.id,
                    IndexedPage.status.in_(("fetched", "failed", "skipped")),
                )
                .values(status="pending", attempts=0, next_attempt_at=None, last_error=None)
            )
        db.commit()

    root = db.scalar(
        select(IndexedPage).where(IndexedPage.site_id == site.id, IndexedPage.url == url)
    )
    if root is None:
        db.add(IndexedPage(site_id=site.id, url=url, depth=0, status="pending"))
        db.commit()
    return site


def _discover_links(html: str, base_url: str, domain: str, same_host_only: bool) -> list[str]:
    soup = BeautifulSoup(html, "lxml")
    out: list[str] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        url = _norm(urljoin(base_url, href))
        if not url.startswith(("http://", "https://")):
            continue
        if _is_asset_url(url):
            continue
        if same_host_only and _domain(url) != domain:
            continue
        if url in seen:
            continue
        seen.add(url)
        out.append(url)
    return out


def _smart_targets(html: str, base_url: str, domain: str, same_host_only: bool) -> dict[str, int]:
    """Smart-crawl link selection: return {url: priority} of links WORTH crawling.

    - drop account/legal/cart junk entirely;
    - collapse chapter links to their parent work's landing URL (so we fetch the rich
      metadata page once instead of indexing thousands of chapter pages);
    - rank work-landing links above listing pages above everything else.
    This is what makes the crawl find *books*, not just any same-host page."""
    targets: dict[str, int] = {}
    for url in _discover_links(html, base_url, domain, same_host_only):
        if is_junk_url(url):
            continue
        if is_noncatalog_content_url(url):
            # A book's full content/file tree (e.g. Gutenberg /files/ , /cache/) — the hooker only
            # needs the catalog landing URL (/ebooks/<id>), so don't spend the frontier on content.
            continue
        if is_chapter_url(url):
            wu = work_url_for(url)
            # Only crawl when the chapter collapses to a DIFFERENT, non-chapter landing (a real
            # work page). A chapter that can't be collapsed — e.g. a j-novel.club /read/ page we
            # can't map to a /series/ URL — is a content-less dead-end (paywalled, 0 words, no
            # title); crawling it just burns a request. Skip it: the work's own landing page is
            # found via listing/series links, so discovery isn't lost. This is the fix for sites
            # (j-novel) that hit 20-30 stale reader fetches before surfacing a single title.
            if wu == url or is_chapter_url(wu):
                continue
            if same_host_only and _domain(wu) != domain:
                continue
            targets[wu] = max(targets.get(wu, 0), 2)
            continue
        targets[url] = max(targets.get(url, 0), link_priority(url))
    return targets


# ---- Resilient fetching: transient failures retry, blocks throttle the site --------------
# A crawl should never lose pages to a passing network blip, a temporary anti-bot block, or a
# spent daily budget. Transient failures keep the page in the frontier (retried with backoff);
# only a genuinely dead URL (404/410), a robots disallow, or attempts-exhausted marks it
# permanently. Sustained pushback cools the whole *site* down for a while, then it resumes —
# the only thing that truly ENDS a crawl is the idle-stop (no new titles), per design.
_MAX_PAGE_ATTEMPTS = 5            # give up on one page after this many transient failures
_PAGE_RETRY_BASE_S = 60          # first page retry ~1 min out, doubling…
_PAGE_RETRY_CAP_S = 1800         # …never more than 30 min between page retries
_SITE_COOLDOWN_BASE_S = 30       # first site-wide cooldown on sustained pushback…
_SITE_COOLDOWN_CAP_S = 1800      # …escalating up to 30 min
_SITE_BLOCK_THRESHOLD = 2        # consecutive errors before the whole site cools down
_BUDGET_COOLDOWN_S = 3600        # daily budget spent → pause the site ~1h, then resume


def _backoff(base: float, cap: float, n: int) -> float:
    """Jittered exponential backoff in seconds: base*2^(n-1), capped, +0..25% jitter."""
    import random

    delay = min(base * (2 ** max(0, n - 1)), cap)
    return delay + delay * 0.25 * random.random()


def _as_utc(dt: datetime | None) -> datetime | None:
    """SQLite hands timezone-naive datetimes back; treat them as UTC for comparisons."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def _cooldown_site(db: Session, site: IndexSite, seconds: float, *, commit: bool = True) -> None:
    """Pause a site for `seconds` (keeping the later of any existing cooldown). The site stays
    'active' — the scheduler just skips it until the cooldown elapses, then it picks back up."""
    until = _utcnow() + timedelta(seconds=seconds)
    cur = _as_utc(site.cooldown_until)
    site.cooldown_until = until if (cur is None or until > cur) else cur
    if commit:
        db.commit()


def _classify_status(status: int) -> str:
    """'blocked' (transient, throttle the site) vs 'permanent' (dead URL, fail just the page)."""
    if status == 429 or status >= 500 or status in (403, 408, 425):
        return "blocked"
    return "permanent"


def _handle_fetch_failure(
    db: Session, page: IndexedPage, site: IndexSite, *, kind: str, detail: str,
    retry_after: float | None = None,
) -> None:
    now = _utcnow()
    # Prefix with the cause category so the UI can explain WHY a request failed at a glance
    # (robots / permanent dead URL / daily budget / transient block).
    page.last_error = f"{kind}: {detail or kind}"[:500]

    if kind in ("robots", "asset"):
        # robots: not allowed to fetch this URL. asset: the URL is a downloadable file (navigating
        # to it made the browser start a download), not a crawlable page. Either way it's a property
        # of the URL, NOT the site pushing back — so stop trying it, with no cooldown and without
        # counting as a failure (otherwise a book's handful of download links would cool the whole
        # site down, which is exactly what stalled the Gutenberg crawl).
        page.status = "skipped"
        page.fetched_at = now
        db.commit()
        return

    if kind == "permanent":
        # A clean 404/410/etc: the site is responding fine, the URL is just dead.
        page.status = "failed"
        page.fetched_at = now
        db.commit()
        return

    if kind == "budget":
        # Daily request budget spent: pacing, not failure. Leave the page pending and pause
        # the whole site for a while; it resumes once the budget window rolls over. Never
        # counts against the page's retry attempts.
        site.last_error = "paused: daily request budget reached — resumes when it rolls over"
        _cooldown_site(db, site, _BUDGET_COOLDOWN_S)
        log.info("index site=%s paused (budget): %s", site.id, detail)
        return

    # kind == "blocked" / unexpected transient error: keep the page in the frontier and retry
    # it later; if errors are piling up, cool the whole site down with escalating backoff.
    page.attempts = (page.attempts or 0) + 1
    if page.attempts >= _MAX_PAGE_ATTEMPTS:
        page.status = "failed"
        page.fetched_at = now
    else:
        page.status = "pending"  # stays in the frontier, just deferred
        page.next_attempt_at = now + timedelta(
            seconds=_backoff(_PAGE_RETRY_BASE_S, _PAGE_RETRY_CAP_S, page.attempts)
        )
    site.consecutive_errors = (site.consecutive_errors or 0) + 1
    if retry_after or site.consecutive_errors >= _SITE_BLOCK_THRESHOLD:
        secs = max(
            retry_after or 0.0,
            _backoff(_SITE_COOLDOWN_BASE_S, _SITE_COOLDOWN_CAP_S, site.consecutive_errors),
        )
        site.last_error = (
            f"cooling down: site pushed back ({site.consecutive_errors} errors in a row) — "
            f"last: {detail or kind}"
        )[:500]
        _cooldown_site(db, site, secs, commit=False)
    db.commit()
    log.info("index fetch deferred %s (attempt %s): %s", page.url, page.attempts, detail)


def _note_fetch_success(db: Session, site: IndexSite) -> None:
    """A clean fetch — clear any pushback state so the crawl speeds back up."""
    if site.consecutive_errors or site.cooldown_until is not None:
        site.consecutive_errors = 0
        site.cooldown_until = None
        db.commit()


# SPA domains whose pages return only an empty shell to a plain HTTP fetch — the crawler must
# JS-render them (and scroll to trigger lazy grids) to see any links/content. Auto-rendering only
# these keeps every other index crawl on the fast plain-HTTP path (no global render_js needed).
_RENDER_DOMAINS = {"comix.to"}


def _needs_render(url: str) -> bool:
    d = (_domain(url) or "").lower()
    if d.startswith("www."):
        d = d[4:]
    return any(d == rd or d.endswith("." + rd) for rd in _RENDER_DOMAINS)


async def _fetch_one(db: Session, src: Source, page: IndexedPage, site: IndexSite) -> None:
    from .fetcher import DailyBudgetExceeded, RobotsDisallowed, _parse_retry_after

    fetcher = get_fetcher()
    # Pace + back off PER DOMAIN, not on one shared 'web_index' budget — so a slow/blocking site
    # (e.g. one returning 5xx) throttles only itself and never starves the other index crawls.
    rate_key = f"{SOURCE_KEY}:{site.domain or _domain(page.url)}"
    render = _needs_render(page.url)
    try:
        resp = await fetcher.get_html(
            SOURCE_KEY, page.url, rate_key=rate_key,
            force_render=render, scroll=(6 if render else 0),
        )
        status = getattr(resp, "status_code", 200)
        if status >= 400:
            ra = _parse_retry_after(getattr(resp, "headers", {}).get("Retry-After")) \
                if hasattr(resp, "headers") else None
            _handle_fetch_failure(db, page, site, kind=_classify_status(status),
                                  detail=f"HTTP {status}", retry_after=ra)
            return
        html = resp.text
    except RobotsDisallowed as exc:
        _handle_fetch_failure(db, page, site, kind="robots", detail=str(exc))
        return
    except DailyBudgetExceeded as exc:
        _handle_fetch_failure(db, page, site, kind="budget", detail=str(exc))
        return
    except Exception as exc:  # noqa: BLE001 — network errors that survived the fetcher's retries
        # Some exceptions (e.g. httpx.ConnectTimeout) stringify to "" — keep the type name.
        detail = str(exc) or type(exc).__name__
        # A navigation that turns into a file download isn't a block — the URL is just an asset
        # (e.g. a Gutenberg .epub/.kindle download link). Skip the page without penalizing the site.
        kind = "asset" if "download is starting" in detail.lower() else "blocked"
        _handle_fetch_failure(db, page, site, kind=kind, detail=detail)
        return

    _note_fetch_success(db, site)
    # Parsing + sanitizing + cataloging a page is CPU-heavy (multiple BeautifulSoup passes).
    # Run it OFF the asyncio event loop so concurrent API requests aren't starved while a
    # crawl tick chews through a page. The DB session is used only inside this worker thread
    # for the duration of the call (the loop doesn't touch it concurrently), which is safe.
    import asyncio

    await asyncio.to_thread(_store_fetched_page, db, page, site, html)


def _store_fetched_page(db: Session, page: IndexedPage, site: IndexSite, html: str) -> None:
    """Synchronous parse → sanitize → persist → catalog → enqueue. Offloaded to a thread."""
    extracted_title, body_html = extract_main_content(html, page.url)
    # For arbitrary web pages, the page's own og:title / <title> is the most reliable
    # display title; fall back to the readability-extracted heading.
    title = og_title(html) or extracted_title or page.url
    meta = page_metadata(html, page.url)
    clean = sanitize_html(body_html)
    # Localize the page's inline images to permanent local copies at crawl time, so the
    # in-app page reader never depends on remote requests (matches hooked-chapter behavior).
    from .. import imagecache
    clean = imagecache.localize_html_images(clean, base_url=page.url)
    text = BeautifulSoup(clean, "lxml").get_text(" ", strip=True)

    page.title = title[:500]
    page.description = meta["description"]
    page.author = meta["author"]
    page.cover_url = meta["cover_url"]
    page.site_name = meta["site_name"]
    page.page_type = meta["type"]
    page.html = clean
    page.text = text
    page.word_count = count_words(clean)
    page.status = "fetched"
    page.fetched_at = _utcnow()
    page.last_error = None
    db.flush()
    # Index author + description alongside the body so searches also match the
    # gathered preview metadata (e.g. "regency manners" from an og:description).
    fts_body = "\n".join(x for x in (meta["author"], meta["description"], text) if x)
    index_fts_upsert(db.connection(), page.id, page.title or "", fts_body)
    if not site.title:
        site.title = meta["site_name"] or title[:500]
    db.commit()

    # Smart catalog: if this page is (part of) a literary work, record/enrich its entry.
    # Track whether this page surfaced a NEW title so the crawl can stop when discovery dries
    # up (rather than at an arbitrary page count).
    titles_before = db.scalar(
        select(func.count(CatalogWork.id)).where(CatalogWork.site_id == site.id)
    ) or 0
    try:
        catalog.upsert_from_page(db, site, html, page.url)
    except Exception:  # never let cataloging break the crawl
        log.exception("catalog upsert failed for %s", page.url)
        db.rollback()
    titles_after = db.scalar(
        select(func.count(CatalogWork.id)).where(CatalogWork.site_id == site.id)
    ) or 0
    found_new_title = titles_after > titles_before
    if found_new_title:
        site.titles_found = titles_after

    # Once a crawl has gone a long stretch finding NOTHING new — no catalog title AND no new
    # link — stop *discovering* further pages, but keep draining whatever's already queued.
    # A site is finished only when its frontier is empty (index_tick), so content we've already
    # found is never abandoned. This is the fix for crawls that were marked "finished" early:
    # a content-heavy site with few/no books no longer stops after N pages while thousands of
    # pages remain un-indexed — every newly-discovered page counts as progress.
    idle_cap = site.stop_after_idle_pages or settings.index_stop_after_idle_pages
    in_dry_spell = bool(idle_cap and (site.pages_since_new_title or 0) >= idle_cap)
    _added, discovered_new = _enqueue_links(db, site, page, html, discover=not in_dry_spell)

    if found_new_title or discovered_new:
        site.pages_since_new_title = 0
    else:
        site.pages_since_new_title = (site.pages_since_new_title or 0) + 1
    db.commit()


def _enqueue_links(
    db: Session, site: IndexSite, page: IndexedPage, html: str, *, discover: bool = True
) -> tuple[int, bool]:
    """Discover same-host links worth crawling from a page and enqueue the new ones (highest
    value first), within the site's depth + frontier bounds.

    Returns (added, discovered_new): how many pages were newly queued, and whether the page
    revealed ANY not-yet-seen crawlable URL at all — reported even when ``discover`` is False or
    the frontier is full, so the caller can tell a still-productive crawl from a true dead-end.
    """
    unlimited = not site.max_pages
    # Depth is just a loop guard (URLs are de-duped); for an unlimited crawl keep it loose so
    # deep pagination / nested sections are still reached.
    max_depth = max(site.max_depth, settings.index_max_depth) if unlimited else site.max_depth
    if page.depth >= max_depth:
        return 0, False

    existing = {
        u for (u,) in db.execute(
            select(IndexedPage.url).where(IndexedPage.site_id == site.id)
        ).all()
    }
    from . import blocklist
    blk_urls, blk_domains = blocklist.blocked_sets(db)  # load once, not per-candidate
    # Highest-priority (work-landing) links first so we spend the frontier on books, not chrome.
    targets = sorted(
        _smart_targets(html, page.url, site.domain, site.same_host_only).items(),
        key=lambda kv: kv[1], reverse=True,
    )
    fresh = [
        (u, prio) for (u, prio) in targets
        if u not in existing and not blocklist.is_blocked_in(u, blk_urls, blk_domains)
    ]
    discovered_new = bool(fresh)
    if not discover or not fresh:
        return 0, discovered_new

    total = db.scalar(
        select(func.count(IndexedPage.id)).where(IndexedPage.site_id == site.id)
    ) or 0
    if not unlimited and total >= site.max_pages:
        return 0, discovered_new
    pending = db.scalar(
        select(func.count(IndexedPage.id)).where(
            IndexedPage.site_id == site.id, IndexedPage.status == "pending"
        )
    ) or 0
    frontier_room = max(0, settings.index_max_pending_frontier - pending)
    added = 0
    for url, prio in fresh:
        if added >= frontier_room or (not unlimited and total >= site.max_pages):
            break
        db.add(IndexedPage(site_id=site.id, url=url, depth=page.depth + 1,
                           priority=prio, status="pending"))
        total += 1
        added += 1
    if added:
        db.commit()
    return added, discovered_new


_DEADEND_RECLAIM_KEY = "reader_deadend_reclaim_v2"


def reclaim_reader_deadends(db: Session) -> dict:
    """One-time backlog cleanup for reader/chapter dead-ends already in the frontier.

    Earlier crawls enqueued lots of content-less reader pages (e.g. j-novel.club /read/ parts —
    paywalled, 0 words, never a title). Going forward ``_smart_targets`` skips them, but the queued
    ones would still be fetched one-by-one (the "20-30 stale requests per title" the user saw). This
    collapses each such page to its work (series) landing: enqueues the work URL if missing (so its
    title gets discovered) and marks the still-pending dead-end ``skipped`` so it's never fetched.
    Idempotent + gated by an app_settings sentinel so it runs once."""
    from ..models import AppSetting

    if db.get(AppSetting, _DEADEND_RECLAIM_KEY) is not None:
        return {"ran": False, "skipped": 0, "enqueued": 0}
    rows = db.execute(
        select(IndexedPage.id, IndexedPage.site_id, IndexedPage.url, IndexedPage.status)
        .where(IndexedPage.status.in_(("pending", "fetched")))
    ).all()
    existing: dict[int, set[str]] = {}
    skipped = enqueued = 0
    for pid, site_id, url, status in rows:
        if not is_chapter_url(url):
            continue
        wu = work_url_for(url)
        if wu == url or is_chapter_url(wu) or not is_work_url(wu):
            continue  # no usable work landing to redirect to
        seen = existing.get(site_id)
        if seen is None:
            seen = {u for (u,) in db.execute(
                select(IndexedPage.url).where(IndexedPage.site_id == site_id))}
            existing[site_id] = seen
        if wu not in seen:
            db.add(IndexedPage(site_id=site_id, url=wu, depth=0, priority=2, status="pending"))
            seen.add(wu)
            enqueued += 1
        if status == "pending":
            db.execute(
                update(IndexedPage).where(IndexedPage.id == pid)
                .values(status="skipped", last_error="reader dead-end → collapsed to work URL")
            )
            skipped += 1
    db.add(AppSetting(key=_DEADEND_RECLAIM_KEY,
                      value={"done": True, "skipped": skipped, "enqueued": enqueued}))
    db.commit()
    log.info("reader dead-end reclaim: skipped=%s, enqueued work pages=%s", skipped, enqueued)
    return {"ran": True, "skipped": skipped, "enqueued": enqueued}


# Sites currently being crawled (a _crawl_site task is in flight). Guards against a tick launching
# a second concurrent crawl of the same site, while letting each site run on its OWN cadence.
_sites_in_progress: set[int] = set()
_bg_tasks: set[asyncio.Task] = set()


async def _crawl_site_bg(site_id: int, batch: int) -> None:
    try:
        await _crawl_site(site_id, batch)
    finally:
        _sites_in_progress.discard(site_id)


async def _drain_crawl_tasks() -> None:
    """Wait for all in-flight per-site crawl tasks to finish (used by tests + clean shutdown)."""
    if _bg_tasks:
        await asyncio.gather(*list(_bg_tasks), return_exceptions=True)


async def index_tick() -> None:
    """Crawl each active index site CONCURRENTLY, INDEPENDENTLY, and DECOUPLED from the tick cadence.

    Each due site is launched as its OWN background task (own session, own per-domain budget). The
    tick does NOT await them: a slow/throttled site (e.g. one backing off for 30s) runs in the
    background while fast sites get relaunched every tick — so one bad site never delays the tick or
    starves the others. A per-site in-progress guard prevents a later tick from double-crawling a
    site whose batch is still running."""
    db = SessionLocal()
    site_ids: list[int] = []
    try:
        src = db.scalar(select(Source).where(Source.key == SOURCE_KEY))
        if src is None or not src.tos_permitted:
            return
        ensure_source(db, _web_index_adapter_cls())  # keep fetcher budget in sync

        from . import crawl_tuning
        # Per-tick page budget applied PER SITE (each site independently fetches up to this many).
        batch = crawl_tuning.get_tuning(db)["parallel_fetches"]
        now = _utcnow()
        # Self-heal: re-activate any "done" site that still has queued pages (e.g. stopped early
        # by the old idle-stop, or a re-index). A genuinely finished site has 0 pending → untouched.
        revived = db.execute(
            update(IndexSite)
            .where(
                IndexSite.status == "done",
                IndexSite.id.in_(
                    select(IndexedPage.site_id).where(IndexedPage.status == "pending")
                ),
            )
            .values(status="active")
        ).rowcount
        if revived:
            db.commit()
            log.info("index: revived %s 'done' site(s) with queued pages", revived)
        # Re-activate API-catalog sites (comix.to) that finished but are due for a refresh pass
        # (a new full page-through picks up titles added since the last sync).
        from . import comix_catalog
        refreshed = 0
        for site in db.scalars(select(IndexSite).where(IndexSite.status == "done")).all():
            if comix_catalog.is_api_catalog_site(site) and comix_catalog.is_due(site, now):
                site.status = "active"
                refreshed += 1
        if refreshed:
            db.commit()
            log.info("index: re-activated %s API-catalog site(s) for refresh", refreshed)
        # Sites to crawl this tick: active and not currently cooling down. Capture ids only — each
        # runs below in its own session.
        for site in db.scalars(select(IndexSite).where(IndexSite.status == "active")).all():
            cd = _as_utc(site.cooldown_until)
            if cd is not None and cd > now:
                continue
            site_ids.append(site.id)
    except Exception:
        log.exception("index tick orchestration failed")
        return
    finally:
        db.close()

    # Launch each due site that isn't already being crawled, as an independent background task.
    for sid in site_ids:
        if sid in _sites_in_progress:
            continue  # its previous batch is still running (e.g. a throttled site) — let it run
        _sites_in_progress.add(sid)
        task = asyncio.create_task(_crawl_site_bg(sid, batch))
        _bg_tasks.add(task)  # keep a ref so the task isn't GC'd; drop it when done
        task.add_done_callback(_bg_tasks.discard)


async def _crawl_site(site_id: int, batch: int) -> None:
    """Fetch up to ``batch`` due pages from ONE site, in its own session, paced by the site's own
    per-domain budget. Isolated: a transient failure or a long adaptive backoff affects only this
    site, never the concurrent crawls of the others."""
    db = SessionLocal()
    try:
        src = db.scalar(select(Source).where(Source.key == SOURCE_KEY))
        site = db.get(IndexSite, site_id)
        if src is None or site is None or site.status != "active":
            return
        # SPA sites whose catalog comes from a JSON API (comix.to) are paged from that API rather
        # than HTML-crawled (the SPA's /browse only renders a slice). Advance a bounded number of
        # catalog pages this pass, then fall through to drain any HTML frontier as usual.
        from . import comix_catalog
        if comix_catalog.is_api_catalog_site(site) and comix_catalog.is_due(site):
            try:
                await comix_catalog.ingest_tick(db, site)
            except Exception:  # noqa: BLE001 — catalog ingest must not break the crawl
                db.rollback()
                log.exception("comix catalog ingest failed site=%s", site.id)
        for _ in range(max(1, batch)):
            now = _utcnow()
            cd = _as_utc(site.cooldown_until)
            if cd is not None and cd > now:
                break  # got cooled down mid-batch (a block) — stop, resume next tick
            page = db.scalar(
                select(IndexedPage)
                .where(
                    IndexedPage.site_id == site.id,
                    IndexedPage.status == "pending",
                    or_(IndexedPage.next_attempt_at.is_(None),
                        IndexedPage.next_attempt_at <= now),
                )
                .order_by(IndexedPage.priority.desc(), IndexedPage.depth, IndexedPage.id)
                .limit(1)
            )
            if page is None:
                # Nothing due. Only END the crawl when the frontier is genuinely empty — pages
                # merely awaiting a retry backoff keep the site active.
                pending_left = db.scalar(
                    select(func.count(IndexedPage.id)).where(
                        IndexedPage.site_id == site.id, IndexedPage.status == "pending"
                    )
                ) or 0
                if pending_left == 0:
                    # An API-catalog site with pages left to ingest (or a refresh due) isn't done.
                    from . import comix_catalog
                    if comix_catalog.is_api_catalog_site(site) and comix_catalog.is_due(site):
                        break  # stay active; ingest more catalog pages next tick
                    site.status = "done"
                    db.commit()
                break
            try:
                await _fetch_one(db, src, page, site)
            except Exception:
                db.rollback()
                log.exception("index fetch failed site=%s url=%s", site.id, page.url)
                break
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        log.exception("index crawl_site failed site=%s", site_id)
    finally:
        db.close()


def _web_index_adapter_cls():
    from .base import registry

    return registry.get(SOURCE_KEY)
