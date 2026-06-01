"""URL indexer — polite auto-crawl of a chosen web location into a searchable store.

A user submits a root URL; ``start_index`` creates an IndexSite + a pending root
page. The scheduler's ``index_tick`` drains pending pages slowly within the
web_index source's rate budget, extracting readable text + same-host links and
enqueueing newly-discovered pages up to the site's page/depth bounds. Every
fetched page is mirrored into the FTS5 index for ranked, snippet-able search.
"""
from __future__ import annotations

import logging
import warnings
from datetime import UTC, datetime
from urllib.parse import urldefrag, urljoin, urlparse

from bs4 import BeautifulSoup

try:  # sitemaps/feeds encountered while crawling parse as XML; silence the noise.
    from bs4 import XMLParsedAsHTMLWarning

    warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
except Exception:  # pragma: no cover
    pass
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import SessionLocal, index_fts_upsert
from ..models import IndexedPage, IndexSite, Source
from ..sanitize import count_words, sanitize_html
from .engine import ComplianceError, ensure_source, get_fetcher
from .extract import extract_main_content, og_title, page_metadata

log = logging.getLogger("shelf.indexer")
settings = get_settings()
SOURCE_KEY = "web_index"

# Don't enqueue obvious non-document assets as pages.
_SKIP_EXTS = (
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico", ".css", ".js",
    ".pdf", ".zip", ".gz", ".mp3", ".mp4", ".mov", ".avi", ".woff", ".woff2",
    ".ttf", ".xml", ".json", ".rss", ".atom",
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _norm(url: str) -> str:
    return urldefrag(url)[0].strip()


def _domain(url: str) -> str:
    return (urlparse(url).netloc or "").lower()


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
) -> IndexSite:
    """Create a site + seed its root page (pending). Raises ComplianceError if disabled."""
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
            status="active",
        )
        db.add(site)
        db.commit()
        db.refresh(site)
    else:
        site.status = "active"
        if max_pages:
            site.max_pages = max_pages
        if max_depth is not None:
            site.max_depth = max_depth
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
        path = urlparse(url).path.lower()
        if path.endswith(_SKIP_EXTS):
            continue
        if same_host_only and _domain(url) != domain:
            continue
        if url in seen:
            continue
        seen.add(url)
        out.append(url)
    return out


async def _fetch_one(db: Session, src: Source, page: IndexedPage, site: IndexSite) -> None:
    fetcher = get_fetcher()
    try:
        resp = await fetcher.get_html(SOURCE_KEY, page.url)
        status = getattr(resp, "status_code", 200)
        if status >= 400:
            raise RuntimeError(f"HTTP {status}")
        html = resp.text
    except Exception as exc:  # noqa: BLE001  (robots-disallowed, network, http errors)
        page.status = "failed"
        # Some exceptions (e.g. httpx.ConnectTimeout) stringify to "" — keep the type.
        page.last_error = (str(exc) or type(exc).__name__)[:500]
        page.fetched_at = _utcnow()
        db.commit()
        log.info("index fetch failed %s: %s", page.url, exc)
        return

    extracted_title, body_html = extract_main_content(html, page.url)
    # For arbitrary web pages, the page's own og:title / <title> is the most reliable
    # display title; fall back to the readability-extracted heading.
    title = og_title(html) or extracted_title or page.url
    meta = page_metadata(html, page.url)
    clean = sanitize_html(body_html)
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

    # Enqueue newly-discovered links within bounds.
    total = db.scalar(select(func.count(IndexedPage.id)).where(IndexedPage.site_id == site.id)) or 0
    if page.depth >= site.max_depth or total >= site.max_pages:
        return
    existing = {
        u for (u,) in db.execute(
            select(IndexedPage.url).where(IndexedPage.site_id == site.id)
        ).all()
    }
    for url in _discover_links(html, page.url, site.domain, site.same_host_only):
        if total >= site.max_pages:
            break
        if url in existing:
            continue
        db.add(IndexedPage(site_id=site.id, url=url, depth=page.depth + 1, status="pending"))
        existing.add(url)
        total += 1
    db.commit()


async def index_tick() -> None:
    """Drain a few pending pages across active sites, politely."""
    db = SessionLocal()
    try:
        src = db.scalar(select(Source).where(Source.key == SOURCE_KEY))
        if src is None or not src.tos_permitted:
            return
        ensure_source(db, _web_index_adapter_cls())  # keep fetcher budget in sync

        active = db.scalars(select(IndexSite).where(IndexSite.status == "active")).all()
        worked = 0
        for site in active:
            page = db.scalar(
                select(IndexedPage)
                .where(IndexedPage.site_id == site.id, IndexedPage.status == "pending")
                .order_by(IndexedPage.depth, IndexedPage.id)
                .limit(1)
            )
            if page is None:
                site.status = "done"
                db.commit()
                continue
            await _fetch_one(db, src, page, site)
            worked += 1
            if worked >= max(1, settings.global_max_concurrency):
                break
    except Exception:
        log.exception("index tick failed")
    finally:
        db.close()


def _web_index_adapter_cls():
    from .base import registry

    return registry.get(SOURCE_KEY)
