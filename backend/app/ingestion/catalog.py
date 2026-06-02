"""The discovered-works catalog — a searchable 'card catalog' built while indexing.

The smart indexer feeds pages in via :func:`upsert_from_page` (only literature pages
become catalog entries). The API reads them back grouped + deduped across sites via
:func:`group_rows` so the same title found on several sources is one card with a
source picker. Hooking a chosen source is handled in :mod:`.diagnose`.
"""
from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..models import CatalogWork, IndexSite, Work
from .base import registry
from .extract import (
    classify_page,
    detect_media_kind,
    norm_title,
    og_title,
    page_metadata,
    titles_match,
    work_title_from,
)

# Page kinds that represent (part of) a literary work worth cataloging.
_LIT_KINDS = ("work", "toc", "chapter")


def _utcnow() -> datetime:
    return datetime.now(UTC)


# Domains with a purpose-built adapter (better metadata + chapterization than the generic
# adaptive crawler); everything else uses generic_feed.
_DOMAIN_ADAPTERS = {
    "mangadex.org": "mangadex",
    "standardebooks.org": "standardebooks",
    "gutenberg.org": "gutenberg",
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
    meta = page_metadata(html, url)
    title = (pc.title or work_title_from(og_title(html)) or work_url)[:512]

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


def upsert_external(db: Session, integration, ext) -> CatalogWork:
    """Create/update a catalog entry from an integration's ExternalWork. Deduped by
    (provider, provider_ref, integration). Copies the metadata the integration pulled."""
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


def _union_find_groups(rows: list[CatalogWork]) -> list[list[CatalogWork]]:
    """Cluster rows by strong title+author matching (not just exact normalized title),
    so the same work from web crawl + Readarr + Kapowarr lands in one group."""
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
    # Exact-key buckets first (cheap), then pairwise fuzzy merge (n bounded by find_rows).
    by_key: dict[str, int] = {}
    for i, k in enumerate(keys):
        if k in by_key:
            union(i, by_key[k])
        else:
            by_key[k] = i
    for i in range(n):
        for j in range(i + 1, n):
            if find(i) == find(j):
                continue
            if titles_match(keys[i], rows[i].author, keys[j], rows[j].author):
                union(i, j)

    clusters: dict[int, list[CatalogWork]] = {}
    for i, r in enumerate(rows):
        clusters.setdefault(find(i), []).append(r)
    return list(clusters.values())


def _source_kind(e: CatalogWork) -> str:
    return "online" if e.provider == "web_index" else e.provider


def group_rows(rows: list[CatalogWork], q: str | None = None) -> list[dict]:
    """Group rows into one entry per work (strong cross-source matching) with a list of
    selectable sources. Returns plain dicts (the router maps them to schemas)."""
    out: list[dict] = []
    for entries in _union_find_groups(rows):
        entries.sort(key=lambda e: _score(e, q), reverse=True)
        best = entries[0]
        sources = [
            {
                "catalog_id": e.id,
                "site_id": e.site_id,
                "domain": e.domain,
                "work_url": e.work_url,
                "provider": e.provider,
                "kind": _source_kind(e),
                "integration_id": e.integration_id,
                "chapters_advertised": e.chapters_advertised,
                "chapters_listed": e.chapters_listed,
                "health": e.health,
                "health_detail": e.health_detail,
                "hooked_work_id": e.hooked_work_id,
                "grab_status": (e.extra or {}).get("grab_status"),
            }
            for e in entries
        ]
        out.append(
            {
                "norm_key": best.norm_key or norm_title(best.title),
                "title": best.title,
                "author": best.author,
                "cover_url": best.cover_url,
                "synopsis": best.synopsis,
                "language": best.language,
                "media_kind": best.media_kind,
                "chapters": best.chapters_advertised or best.chapters_listed,
                "hooked_work_id": next(
                    (e.hooked_work_id for e in entries if e.hooked_work_id), None
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


async def hook_entry(db: Session, entry: CatalogWork) -> Work:
    """Move a catalog entry into the library: pull it via the adaptive web adapter,
    self-troubleshoot if no chapters surface, carry over the catalog's metadata, and
    record a completeness health verdict on both the Work and the catalog entry.

    Raises engine.ComplianceError (→ HTTP 403) if the adaptive-web source isn't enabled.
    """
    from . import diagnose
    from .engine import ComplianceError, adapter_for, ensure_source, hook_work

    # Some domains have a purpose-built adapter (e.g. MangaDex's API) that ingests far
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
