"""The discovered-works catalog — a searchable 'card catalog' built while indexing.

The smart indexer feeds pages in via :func:`upsert_from_page` (only literature pages
become catalog entries). The API reads them back grouped + deduped across sites via
:func:`group_rows` so the same title found on several sources is one card with a
source picker. Hooking a chosen source is handled in :mod:`.diagnose`.
"""
from __future__ import annotations

import re
from datetime import UTC, datetime
from urllib.parse import urlparse

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..models import CatalogWork, IndexSite, Work
from .base import registry
from .extract import (
    _is_gutenberg_book,
    _is_site_name_title,
    classify_page,
    detect_media_kind,
    is_junk_url,
    is_listing_url,
    norm_title,
    og_title,
    page_metadata,
    split_byline,
    titles_match,
    work_title_from,
    work_url_for,
)

# Page kinds that represent (part of) a literary work worth cataloging.
_LIT_KINDS = ("work", "toc", "chapter")


def _utcnow() -> datetime:
    return datetime.now(UTC)


# Domains with a purpose-built adapter (better metadata + chapterization than the generic
# adaptive crawler); everything else uses generic_feed.
_DOMAIN_ADAPTERS = {
    "standardebooks.org": "standardebooks",
    "gutenberg.org": "gutenberg",
    "j-novel.club": "jnovel",
    "comix.to": "comix",
}


def _source_key_for(entry: CatalogWork) -> str:
    # Match on host suffix (not substring) so look-alikes like notgutenberg.org or
    # evil.com/?ref=gutenberg.org can't misroute to a dedicated adapter.
    dom = (entry.domain or "").lower()
    if dom.startswith("www."):
        dom = dom[4:]
    for needle, key in _DOMAIN_ADAPTERS.items():
        if dom == needle or dom.endswith("." + needle):
            return key
    return "generic_feed"


def upsert_from_page(db: Session, site: IndexSite, html: str, url: str) -> CatalogWork | None:
    """If a fetched page is literature, create/update its catalog entry and return it.

    A chapter page is attributed to its *parent work* (so many chapters collapse to one
    catalog entry). Returns None for non-literature pages (browse/account/legal/home).
    Richer fields (cover/synopsis/advertised count) are only *upgraded*, never blanked,
    so a later landing-page fetch enriches an entry first seen via a chapter page.
    """
    pc = classify_page(html, url)
    if pc.kind not in _LIT_KINDS:
        return None
    work_url = pc.work_url or url
    # Operator-removed (blocked) content must not be re-catalogued by a later crawl.
    from . import blocklist
    if blocklist.is_blocked(db, work_url) or blocklist.is_blocked(db, url):
        return None
    meta = page_metadata(html, url)
    title = (pc.title or work_title_from(og_title(html)) or work_url)[:512]
    # Project Gutenberg puts the byline in the page title with no separate author field —
    # "Moby Dick; Or, The Whale by Herman Melville". Split it so the card shows a clean
    # title + author. Scoped to Gutenberg, where "Title by Author" is a reliable convention
    # (splitting blindly would mangle real titles like "Surrounded by Idiots").
    byline_author = None
    if not meta.get("author") and _is_gutenberg_book(url):
        work_title, byline_author = split_byline(title)
        if byline_author:
            title = work_title[:512]

    entry = db.scalar(
        select(CatalogWork).where(
            CatalogWork.site_id == site.id, CatalogWork.work_url == work_url
        )
    )
    created = entry is None
    if entry is None:
        entry = CatalogWork(site_id=site.id, work_url=work_url, domain=site.domain, title=title)
        db.add(entry)

    # A real landing/TOC page is the authoritative title; don't let a later chapter
    # page (whose og:title is "… Chapter N") clobber a good work title.
    if pc.kind in ("work", "toc") or created or not entry.norm_key:
        entry.title = title
        entry.norm_key = norm_title(title)
    if pc.kind in ("work", "toc"):
        entry.kind = "work"
    elif created:
        entry.kind = "work"  # attribute chapter→parent work

    # Upgrade-only enrichment.
    if meta.get("author") and not entry.author:
        entry.author = meta["author"]
    elif byline_author and not entry.author:
        entry.author = byline_author
    if meta.get("cover_url") and not entry.cover_url:
        entry.cover_url = meta["cover_url"]
    if meta.get("description") and (
        not entry.synopsis or len(meta["description"]) > len(entry.synopsis)
    ):
        entry.synopsis = meta["description"]
    if meta.get("language") and not entry.language:
        entry.language = meta["language"]
    # Detect comic/manga so the library + reader treat it as images, not prose. Upgrade
    # text→comic when any page of the work shows a comic signal (never downgrade).
    if entry.media_kind != "comic":
        mk = detect_media_kind(
            url, og_type=meta.get("type"), site_name=meta.get("site_name"), title=title
        )
        if mk == "comic":
            entry.media_kind = "comic"
    if pc.advertised:
        entry.chapters_advertised = max(entry.chapters_advertised or 0, pc.advertised)
    if pc.listed:
        entry.chapters_listed = max(entry.chapters_listed or 0, pc.listed)
    entry.updated_at = _utcnow()
    db.commit()
    db.refresh(entry)
    return entry


def upsert_external(db: Session, integration, ext) -> CatalogWork | None:
    """Create/update a catalog entry from an integration's ExternalWork. Deduped by
    (provider, provider_ref, integration). Copies the metadata the integration pulled.
    Returns None when the work is on the operator blocklist (so a sync can't re-add it)."""
    from . import blocklist
    if ext.url and blocklist.is_blocked(db, ext.url):
        return None
    ref = ext.ref or f"title:{norm_title(ext.title)}"
    entry = db.scalar(
        select(CatalogWork).where(
            CatalogWork.provider == ext.provider,
            CatalogWork.provider_ref == ref,
            CatalogWork.integration_id == integration.id,
        )
    )
    if entry is None:
        entry = CatalogWork(
            provider=ext.provider, provider_ref=ref, integration_id=integration.id,
            domain=integration.name or integration.kind,
            work_url=ext.url or f"{ext.provider}:{ref}", title=ext.title[:512],
        )
        db.add(entry)
    entry.title = ext.title[:512]
    entry.norm_key = norm_title(ext.title)
    if ext.author:
        entry.author = ext.author[:255]
    if ext.cover_url:
        entry.cover_url = ext.cover_url
    if ext.overview and (not entry.synopsis or len(ext.overview) > len(entry.synopsis)):
        entry.synopsis = ext.overview
    entry.media_kind = ext.media_kind
    entry.kind = "work"
    if ext.url:
        entry.work_url = ext.url
    entry.extra = {
        **(ext.extra or {}),
        "in_library": ext.in_library,
        "downloaded": ext.downloaded,
        "integration_kind": integration.kind,
        "root_folder": integration.root_folder,
        "year": ext.year,
    }
    entry.updated_at = _utcnow()
    db.commit()
    db.refresh(entry)
    return entry


def find_rows(
    db: Session,
    *,
    q: str | None = None,
    site_id: int | None = None,
    hooked: bool | None = None,
    limit: int = 600,
) -> list[CatalogWork]:
    """Fetch candidate catalog rows (pre-grouping). ``q`` is a case-insensitive LIKE
    over title/author/synopsis/normalized-key."""
    sel = select(CatalogWork)
    if site_id is not None:
        sel = sel.where(CatalogWork.site_id == site_id)
    if hooked is True:
        sel = sel.where(CatalogWork.hooked_work_id.is_not(None))
    elif hooked is False:
        sel = sel.where(CatalogWork.hooked_work_id.is_(None))
    if q:
        like = f"%{q.strip()}%"
        nk = f"%{norm_title(q)}%"
        sel = sel.where(
            or_(
                CatalogWork.title.ilike(like),
                CatalogWork.author.ilike(like),
                CatalogWork.synopsis.ilike(like),
                CatalogWork.norm_key.like(nk),
            )
        )
    sel = sel.order_by(CatalogWork.updated_at.desc()).limit(limit)
    return list(db.scalars(sel).all())


def _score(entry: CatalogWork, q: str | None) -> tuple:
    """Rank an entry as the representative of its group / for search ordering."""
    chapters = entry.chapters_advertised or entry.chapters_listed or 0
    title_hit = 0
    if q:
        ql = q.lower()
        if ql in (entry.title or "").lower():
            title_hit = 2
        elif ql in (entry.author or "").lower():
            title_hit = 1
    return (title_hit, bool(entry.synopsis), bool(entry.cover_url), chapters)


def _media_bucket(e: CatalogWork) -> str:
    """Coarse media class for grouping: comics never merge with prose."""
    return "comic" if (e.media_kind or "text") == "comic" else "text"


def _union_find_groups(rows: list[CatalogWork]) -> list[list[CatalogWork]]:
    """Cluster rows by strong title+author matching (not just exact normalized title),
    so the same work from web crawl + Readarr + Kapowarr lands in one group.

    Two entries only merge when they're the SAME media class (a novel and its manga
    adaptation share a title but are different works) AND their titles+authors match — so
    e.g. 'My Next Life as a Villainess' the light novel and the manga stay distinct cards."""
    n = len(rows)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        parent[find(i)] = find(j)

    keys = [r.norm_key or norm_title(r.title) for r in rows]
    media = [_media_bucket(r) for r in rows]
    # Exact-key buckets first (cheap), then pairwise fuzzy merge (n bounded by find_rows).
    # Bucket by (normalized title, media class) so a novel and its comic adaptation don't
    # collapse into one card just because the title strings are identical.
    by_key: dict[tuple[str, str], int] = {}
    for i, k in enumerate(keys):
        bk = (k, media[i])
        if bk in by_key:
            union(i, by_key[bk])
        else:
            by_key[bk] = i
    for i in range(n):
        for j in range(i + 1, n):
            if media[i] != media[j] or find(i) == find(j):
                continue
            if titles_match(keys[i], rows[i].author, keys[j], rows[j].author):
                union(i, j)

    clusters: dict[int, list[CatalogWork]] = {}
    for i, r in enumerate(rows):
        clusters.setdefault(find(i), []).append(r)
    return list(clusters.values())


def _source_kind(e: CatalogWork) -> str:
    return "online" if e.provider == "web_index" else e.provider


_MANGA_RE = re.compile(r"manga|manhwa|manhua", re.I)
_WEBTOON_RE = re.compile(r"webtoon|\btoon", re.I)


def media_label(e: CatalogWork) -> str:
    """A human label for what a source actually is — so the user knows whether they're
    hooking a Novel, a Book, Manga, a Webtoon, or a Comic (not just 'text'/'comic')."""
    dom = (e.domain or "").lower()
    hay = f"{dom} {(e.work_url or '').lower()} {(e.title or '').lower()}"
    if _media_bucket(e) == "comic":
        if _MANGA_RE.search(hay):
            return "Manga"
        if _WEBTOON_RE.search(hay):
            return "Webtoon"
        return "Comic"
    if dom.endswith("gutenberg.org") or "standardebooks" in dom or e.provider == "readarr":
        return "Book"
    return "Novel"


def group_rows(rows: list[CatalogWork], q: str | None = None) -> list[dict]:
    """Group rows into one entry per work (strong cross-source matching) with a list of
    selectable sources. Returns plain dicts (the router maps them to schemas)."""
    out: list[dict] = []
    for entries in _union_find_groups(rows):
        entries.sort(key=lambda e: _score(e, q), reverse=True)
        # Collapse duplicate sources from the same domain (e.g. a work re-discovered under
        # two URLs on one site) — keep the richest. Different domains stay separate sources.
        seen_domains: set[str] = set()
        deduped: list[CatalogWork] = []
        for e in entries:
            dom = (e.domain or "").lower()
            if dom and dom in seen_domains:
                continue
            seen_domains.add(dom)
            deduped.append(e)
        best = deduped[0]
        sources = [
            {
                "catalog_id": e.id,
                # Each source's OWN title/author/cover/synopsis (the "sub-title" a given
                # site matched) so the user can compare and pick the right one to hook.
                "title": e.title,
                "author": e.author,
                "cover_url": e.cover_url,
                "synopsis": e.synopsis,
                "site_id": e.site_id,
                "domain": e.domain,
                "work_url": e.work_url,
                "provider": e.provider,
                "kind": _source_kind(e),
                # What this source actually is, so the user knows what they're hooking.
                "media_kind": e.media_kind,
                "media_label": media_label(e),
                "integration_id": e.integration_id,
                "chapters_advertised": e.chapters_advertised,
                "chapters_listed": e.chapters_listed,
                "health": e.health,
                "health_detail": e.health_detail,
                "hooked_work_id": e.hooked_work_id,
                "grab_status": (e.extra or {}).get("grab_status"),
            }
            for e in deduped
        ]
        out.append(
            {
                # Stable unique id for the group (the representative's catalog id) so the UI
                # has a collision-free React key — two same-titled works of different media
                # share a norm_key, which previously caused duplicated/again rendered cards.
                "id": best.id,
                "norm_key": best.norm_key or norm_title(best.title),
                "title": best.title,
                "author": best.author,
                "cover_url": best.cover_url,
                "synopsis": best.synopsis,
                "language": best.language,
                "media_kind": best.media_kind,
                "media_label": media_label(best),
                "chapters": best.chapters_advertised or best.chapters_listed,
                "hooked_work_id": next(
                    (e.hooked_work_id for e in deduped if e.hooked_work_id), None
                ),
                "sources": sources,
            }
        )
    # Sort groups by their best representative's score (relevance, then size).
    def group_key(g: dict) -> tuple:
        ch = g["chapters"] or 0
        title_hit = 1 if (q and q.lower() in (g["title"] or "").lower()) else 0
        return (title_hit, bool(g["synopsis"]), ch)

    out.sort(key=group_key, reverse=True)
    return out


_READ_PREFIX_RE = re.compile(r"^(?:read|watch|listen to)\s+(?=\S)", re.I)


def recanonicalize_catalog(db: Session) -> dict:
    """One-time repair of crawled catalog rows using the *current* heuristics.

    Older crawls cataloged things the smarter classifier now rejects: each j-novel.club
    ``/read/<slug>-volume-N-part-M`` reader page became its own "Read <work>" entry, a
    site homepage became a bogus work, and Gutenberg bylines stayed glued to the title.
    This re-derives the canonical work URL / title / author for every ``web_index`` row,
    merges rows that now collapse onto the same work (carrying over the richest metadata),
    and drops site-root entries. Safe to run repeatedly (idempotent). Returns a summary."""
    rows = list(
        db.scalars(select(CatalogWork).where(CatalogWork.provider == "web_index")).all()
    )
    deleted_roots = merged = retitled = reauthored = 0

    groups: dict[tuple[int | None, str], list[CatalogWork]] = {}
    for e in rows:
        canon = work_url_for(e.work_url)
        # Drop pages that are not works at all: the site root/homepage, browse/genre/author/
        # bookshelf listing pages (e.g. Gutenberg /ebooks/author/<id>), and entries whose
        # title is just the site's own name or a generic chrome label ("Project Gutenberg").
        if (
            not urlparse(e.work_url).path.strip("/")
            or is_listing_url(canon)
            or is_junk_url(canon)
            or _is_site_name_title(e.title, None)
        ):
            db.delete(e)
            deleted_roots += 1
            continue
        groups.setdefault((e.site_id, canon), []).append(e)
    db.flush()

    for (_site_id, canon), entries in groups.items():
        # The surviving row is the one already AT the canonical URL (the real landing page),
        # else the richest by metadata score.
        entries.sort(key=lambda e: (e.work_url == canon, _score(e, None)), reverse=True)
        survivor = entries[0]
        for other in entries[1:]:
            survivor.author = survivor.author or other.author
            survivor.cover_url = survivor.cover_url or other.cover_url
            if other.synopsis and (
                not survivor.synopsis or len(other.synopsis) > len(survivor.synopsis)
            ):
                survivor.synopsis = other.synopsis
            survivor.chapters_advertised = (
                max(survivor.chapters_advertised or 0, other.chapters_advertised or 0) or None
            )
            survivor.chapters_listed = (
                max(survivor.chapters_listed or 0, other.chapters_listed or 0) or None
            )
            if other.hooked_work_id and not survivor.hooked_work_id:
                survivor.hooked_work_id = other.hooked_work_id
            if other.media_kind == "comic":
                survivor.media_kind = "comic"
            db.delete(other)
            merged += 1
        survivor.work_url = canon

        new_title = survivor.title
        if _is_gutenberg_book(canon) and not survivor.author:
            wt, author = split_byline(new_title)
            if author:
                new_title, survivor.author = wt, author
                reauthored += 1
        else:  # reader pages titled "Read <work>" → just the work title
            new_title = _READ_PREFIX_RE.sub("", new_title)
        new_title = (new_title or survivor.title)[:512]
        if new_title != survivor.title:
            survivor.title = new_title
            retitled += 1
        survivor.norm_key = norm_title(survivor.title)

    db.commit()
    return {
        "deleted_roots": deleted_roots,
        "merged": merged,
        "retitled": retitled,
        "reauthored": reauthored,
        "remaining": db.scalar(
            select(func.count(CatalogWork.id)).where(CatalogWork.provider == "web_index")
        ),
    }


def filter_and_sort_groups(
    groups: list[dict], *, media: str | None = None, domain: str | None = None,
    sort: str = "relevance",
) -> list[dict]:
    """Apply the Index page's media-type / source filters and sort to grouped catalog rows.
    Done over the FULL grouped set (server-side) so a low-ranked type/source (e.g. Gutenberg
    books) isn't silently dropped just because it fell outside the first page of results."""
    out = groups
    if media:
        out = [g for g in out if g.get("media_label") == media]
    if domain:
        out = [g for g in out if any(s.get("domain") == domain for s in g.get("sources", []))]
    if sort == "chapters":
        out = sorted(out, key=lambda g: g.get("chapters") or 0, reverse=True)
    elif sort == "title":
        out = sorted(out, key=lambda g: (g.get("title") or "").lower())
    # "relevance" → keep group_rows' existing ordering.
    return out


def catalog_facets(db: Session) -> dict:
    """All distinct media types + source domains across the WHOLE catalog, so the Index
    page's filter dropdowns are complete (not limited to the first page of results)."""
    rows = find_rows(db, limit=5000)
    media = sorted({media_label(r) for r in rows})
    domains = sorted({r.domain for r in rows if r.domain})
    return {"media": media, "domains": domains}


async def hook_entry(db: Session, entry: CatalogWork) -> Work:
    """Move a catalog entry into the library: pull it via the adaptive web adapter,
    self-troubleshoot if no chapters surface, carry over the catalog's metadata, and
    record a completeness health verdict on both the Work and the catalog entry.

    Raises engine.ComplianceError (→ HTTP 403) if the adaptive-web source isn't enabled.
    """
    from . import blocklist, diagnose
    from .engine import ComplianceError, adapter_for, ensure_source, hook_work

    if blocklist.is_blocked(db, entry.work_url):
        raise ComplianceError("This title is on the operator blocklist and can't be hooked.")

    # Some domains have a purpose-built adapter (e.g. a site's own API) that ingests far
    # better than the generic adaptive-web crawler; route to it when recognized.
    source_key = _source_key_for(entry)
    src = ensure_source(db, registry.get(source_key))
    if not src.tos_permitted:
        label = registry.get(source_key).display_name
        raise ComplianceError(
            f"The '{label}' source must be enabled (and attested) on the Sources page "
            "before hooking this work."
        )

    work = await hook_work(db, source_key, entry.work_url)

    # Self-troubleshoot: we thought we found a title but discovery yielded no chapters.
    if not work.chapters:
        try:
            adapter = adapter_for(src)
            await diagnose.troubleshoot_discovery(db, work, adapter, entry.work_url)
            db.refresh(work)
        except Exception:  # noqa: BLE001 — diagnosis is best-effort
            db.rollback()

    # Carry catalog metadata the page-level discovery may have missed.
    if entry.cover_url and not work.cover_url:
        work.cover_url = entry.cover_url
    if entry.synopsis and not work.description:
        work.description = entry.synopsis
    if entry.author and not work.author:
        work.author = entry.author
    if entry.media_kind and work.media_kind == "text":
        work.media_kind = entry.media_kind

    entry.hooked_work_id = work.id
    report = diagnose.completeness(db, work)
    diagnose.apply_health(db, work, report)
    entry.health = report["health"]
    entry.health_detail = (report.get("detail") or "")[:1000] or None
    entry.diagnosed_at = _utcnow()
    db.commit()
    db.refresh(work)
    return work
