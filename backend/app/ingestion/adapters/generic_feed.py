"""Generic feed + adaptive web adapter (Stage 8, extended).

Modes, auto-detected from the supplied ref:
  * Feed mode — RSS / Atom / OPDS URL parsed with feedparser; each entry is a chapter.
  * Web (full TOC) mode — a chapter-index page whose links enumerate every chapter.
  * Web (sequential) mode — for sites whose TOC is paginated/dynamic and not fully
    enumerable politely (e.g. a /ajax-loaded dropdown). We seed the first chapter and
    crawl forward via each page's "next chapter" link, stitching multi-page chapters.

When the source has `render_js` enabled, pages are fetched through the headless
browser (handles JS rendering and passive anti-bot challenges).

This adapter REQUIRES operator attestation and obeys robots.txt + rate limits.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

import feedparser
from bs4 import BeautifulSoup

from ..base import ChapterRef, ComplianceDeclaration, RawChapter, SourceAdapter, WorkMeta, registry
from ..extract import (
    advertised_chapter_count,
    chapter_number,
    chapter_title_from,
    extract_main_content,
    find_chapter_links,
    find_next_targets,
    is_chapter_url,
    looks_paginated_toc,
    og_image,
    og_title,
    series_prefix,
    synthesize_next_chapter_url,
    work_title_from,
)

# Below this many characters of body text, a "chapter" is treated as end-of-serial
# (e.g. a 404 / empty SPA route) so synthesized sequential crawling stops.
_MIN_CHAPTER_CHARS = 200

# Per-process caches.
_FEED_CACHE: dict[str, feedparser.FeedParserDict] = {}
_TOC_CACHE: dict[str, list[tuple[str, str]]] = {}
_SEQ: dict[str, dict] = {}  # work ref -> {"sequential": bool, "first_url": str, "first_title": str}

_MAX_PAGES_PER_CHAPTER = 15


def _novel_page_of(url: str) -> str | None:
    """Given a chapter URL, the likely novel/TOC page URL (strip the /chapter… part)."""
    clean = url.split("#", 1)[0].split("?", 1)[0]
    stripped = re.sub(r"/chapters?(?:[/-].*)?$", "", clean, flags=re.I)
    return stripped if stripped and stripped != clean else None


def _looks_like_feed(text: str, content_type: str) -> bool:
    ct = (content_type or "").lower()
    if any(x in ct for x in ("xml", "rss", "atom", "opds")):
        return True
    head = text[:512].lower()
    return "<rss" in head or "<feed" in head or "<?xml" in head


@registry.register
class GenericFeedAdapter(SourceAdapter):
    key = "generic_feed"
    display_name = "Generic feed / adaptive web"
    description = (
        "User-supplied RSS/Atom/OPDS feed OR a chapter-index page (full or sequential). "
        "Requires you to attest you are permitted to ingest the target. Obeys robots.txt "
        "and rate limits; can use a headless browser when the source enables render_js."
    )
    base_url = None
    enabled = True
    compliance = ComplianceDeclaration(
        license_basis="user-attested",
        tos_permitted_default=False,  # operator must explicitly enable + attest
        robots_respected=True,
        needs_attestation=True,
        min_request_interval_s=8.0,
        max_daily_requests=200,
    )

    async def _classify(self, ref: str) -> tuple[str, str]:
        """Return ('feed'|'web', body_text). Fetches the ref once."""
        page = await self.fetcher.get_html(self.key, ref)
        page.raise_for_status()
        # A render_js source is always treated as web (browsers don't expose feed headers).
        if self.fetcher.is_rendered(self.key):
            return "web", page.text
        headers = getattr(page, "headers", {}) or {}
        ctype = headers.get("content-type", "")
        mode = "feed" if _looks_like_feed(page.text, ctype) else "web"
        return mode, page.text

    # ---- discovery -------------------------------------------------------

    async def discover_work(self, ref: str) -> WorkMeta:
        mode, body = await self._classify(ref)
        if mode == "feed":
            parsed = feedparser.parse(body)
            _FEED_CACHE[ref] = parsed
            feed = parsed.feed
            return WorkMeta(
                source_work_ref=ref,
                title=feed.get("title", ref),
                author=feed.get("author"),
                description=feed.get("subtitle") or feed.get("description"),
                language=feed.get("language", "en"),
                status="ongoing",
            )

        clean_title = work_title_from(og_title(body)) or ref
        cover = og_image(body, ref)
        expected = advertised_chapter_count(body)

        # Case 1: the ref is itself a chapter URL — seed directly from it. Fetch the
        # novel page once for richer metadata (title, cover, total chapter count).
        if is_chapter_url(ref):
            novel_page = _novel_page_of(ref)
            if novel_page and novel_page != ref:
                try:
                    np = await self.fetcher.get_html(self.key, novel_page)
                    np.raise_for_status()
                    nbody = np.text
                    # Keep the (cleaner) chapter-page title; novel page just adds
                    # the cover + the advertised total chapter count.
                    cover = og_image(nbody, novel_page) or cover
                    expected = advertised_chapter_count(nbody) or expected
                except Exception:
                    pass
            _SEQ[ref] = {"sequential": True, "first_url": ref, "first_title": "Chapter 1"}
            return WorkMeta(
                source_work_ref=ref, title=clean_title, cover_url=cover,
                total_chapters_expected=expected, language="en", status="ongoing",
            )

        # Case 2: a novel / TOC page. Restrict chapter links to THIS work's own path
        # (so sidebar recommendations to other novels are ignored).
        links = find_chapter_links(body, ref)
        novel_path = urlparse(ref).path.rstrip("/")
        own = [(u, t) for (u, t) in links if urlparse(u).path.startswith(novel_path + "/")]
        own = own or links
        _TOC_CACHE[ref] = own

        sequential = looks_paginated_toc(body, len(own)) or self.fetcher.is_rendered(self.key)
        if sequential and own:
            first = min(own, key=lambda lt: chapter_number(lt[0]) or chapter_number(lt[1]) or 1e9)
            _SEQ[ref] = {"sequential": True, "first_url": first[0], "first_title": first[1]}
        else:
            _SEQ[ref] = {"sequential": False}
        return WorkMeta(
            source_work_ref=ref,
            title=clean_title,
            author=None,
            description=None,
            cover_url=cover,
            total_chapters_expected=expected,
            language="en",
            status="ongoing",
        )

    async def list_chapters(self, meta: WorkMeta) -> list[ChapterRef]:
        ref = meta.source_work_ref
        # Feed mode.
        parsed = _FEED_CACHE.get(ref)
        if parsed is None and ref not in _TOC_CACHE and ref not in _SEQ:
            await self.discover_work(meta)
            parsed = _FEED_CACHE.get(ref)
        if parsed is not None:
            entries = list(reversed(parsed.entries))  # feeds list newest-first
            return [
                ChapterRef(
                    source_chapter_ref=e.get("link") or e.get("id") or f"entry-{i}",
                    index=i + 1,
                    title=e.get("title", f"Chapter {i+1}"),
                    published_at=e.get("published"),
                )
                for i, e in enumerate(entries)
            ]

        seq = _SEQ.get(ref, {})
        if seq.get("sequential"):
            # Seed a single chapter; the scheduler streams the rest via next-links.
            return [
                ChapterRef(
                    source_chapter_ref=seq["first_url"],
                    index=1,
                    title=seq.get("first_title") or "Chapter 1",
                )
            ]

        links = _TOC_CACHE.get(ref, [])
        return [
            ChapterRef(source_chapter_ref=url, index=i + 1, title=title)
            for i, (url, title) in enumerate(links)
        ]

    # ---- fetching --------------------------------------------------------

    async def fetch_chapter(self, ref: ChapterRef) -> RawChapter:
        src = ref.source_chapter_ref

        # Feed entries may carry full content inline (no extra fetch needed).
        for parsed in _FEED_CACHE.values():
            for e in parsed.entries:
                if (e.get("link") or e.get("id")) == src:
                    content = ""
                    if e.get("content"):
                        content = e["content"][0].get("value", "")
                    content = content or e.get("summary", "")
                    if content.strip():
                        return RawChapter(title=e.get("title", ref.title), body=content, fmt="html")

        # Web mode: fetch the page (rendered if render_js), stitch in-chapter pages,
        # and surface the next-chapter link for sequential crawling.
        page = await self.fetcher.get_html(self.key, src)
        page.raise_for_status()
        title, html_body = extract_main_content(page.text, src)
        next_chapter, next_title, next_page = find_next_targets(page.text, src)

        bodies = [html_body]
        visited = {src.rstrip("/")}
        pages = 1
        while next_page and next_page.rstrip("/") not in visited and pages < _MAX_PAGES_PER_CHAPTER:
            visited.add(next_page.rstrip("/"))
            sub = await self.fetcher.get_html(self.key, next_page)
            try:
                sub.raise_for_status()
            except Exception:
                break
            _t, sub_body = extract_main_content(sub.text, next_page)
            bodies.append(sub_body)
            next_chapter, next_title, next_page = find_next_targets(sub.text, next_page)
            pages += 1

        body = "".join(bodies)
        text_len = len(BeautifulSoup(body, "lxml").get_text(" ", strip=True))

        # Only trust a scraped next-link if it stays within this work's chapter path
        # (otherwise site chrome like "next page" of a ranking list leaks in).
        prefix = series_prefix(src)
        if next_chapter and prefix and not next_chapter.startswith(prefix):
            next_chapter, next_title = None, None

        # Prefer deterministic numeric synthesis for sequential numeric-URL works;
        # fall back to a same-series scraped link. Stop if the page is too short.
        if text_len < _MIN_CHAPTER_CHARS:
            next_chapter = None
        else:
            synth = synthesize_next_chapter_url(src)
            if synth:
                next_chapter, next_title = synth, None
            # else keep the (already series-constrained) scraped next_chapter

        # Prefer the page's own "Chapter N: Subtitle" label for this chapter.
        page_chapter_title = chapter_title_from(og_title(page.text))
        return RawChapter(
            title=page_chapter_title or ref.title or title,
            body=body,
            fmt="html",
            next_ref=next_chapter,
            next_title=next_title,
        )
