"""Ingestion engine (Stage 6/7): adapter resolution, compliance gate, hooking."""
from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import Chapter, ChapterContent, CrawlJob, Source, Work
from ..sanitize import count_words, sanitize_html, text_to_html
from .base import RawChapter, SourceAdapter, registry
from .fetcher import PoliteFetcher

settings = get_settings()
_fetcher: PoliteFetcher | None = None


class ComplianceError(PermissionError):
    """Raised when the engine refuses to run a non-permitted source."""


def get_fetcher() -> PoliteFetcher:
    global _fetcher
    if _fetcher is None:
        _fetcher = PoliteFetcher(
            user_agent=settings.user_agent,
            contact_email=settings.contact_email,
            global_max_concurrency=settings.global_max_concurrency,
        )
    return _fetcher


def _utcnow() -> datetime:
    return datetime.now(UTC)


def ensure_source(db: Session, adapter_cls: type[SourceAdapter]) -> Source:
    """Get or create the Source row backing an adapter, seeded from its compliance defaults."""
    src = db.scalar(select(Source).where(Source.key == adapter_cls.key))
    if src is None:
        c = adapter_cls.compliance
        src = Source(
            key=adapter_cls.key,
            display_name=adapter_cls.display_name,
            base_url=adapter_cls.base_url,
            adapter_key=adapter_cls.key,
            license_basis=c.license_basis,
            tos_permitted=c.tos_permitted_default,
            robots_respected=c.robots_respected,
            min_request_interval_s=c.min_request_interval_s,
            max_daily_requests=c.max_daily_requests,
        )
        db.add(src)
        db.commit()
        db.refresh(src)
    # Keep the fetcher budget in sync with the (operator-editable) Source row.
    get_fetcher().configure_source(
        src.key, src.min_request_interval_s, src.max_daily_requests, src.robots_respected,
        render_js=src.render_js,
    )
    return src


def sync_all_sources(db: Session) -> None:
    for adapter_cls in registry.all():
        ensure_source(db, adapter_cls)


def _gate(src: Source) -> None:
    if not src.tos_permitted:
        raise ComplianceError(
            f"Source {src.key!r} is not marked tos_permitted; refusing to ingest. "
            "Enable it only for sources you are permitted to read."
        )


def adapter_for(src: Source) -> SourceAdapter:
    adapter_cls = registry.get(src.adapter_key)
    if not adapter_cls.enabled:
        raise ComplianceError(f"Adapter {src.adapter_key!r} is disabled.")
    return adapter_cls(get_fetcher())


async def hook_work(db: Session, source_key: str, work_ref: str) -> Work:
    """Run one polite discovery pass, create Work + pending Chapters, enqueue backfill."""
    adapter_cls = registry.get(source_key)
    src = ensure_source(db, adapter_cls)
    _gate(src)
    adapter = adapter_for(src)

    meta = await adapter.discover_work(work_ref)

    work = db.scalar(
        select(Work).where(Work.source_id == src.id, Work.source_work_ref == meta.source_work_ref)
    )
    if work is None:
        work = Work(source_id=src.id, source_work_ref=meta.source_work_ref)
        db.add(work)
    work.title = meta.title
    work.author = meta.author
    work.description = meta.description
    work.cover_url = meta.cover_url
    work.language = meta.language
    work.status = meta.status
    if meta.total_chapters_expected:
        work.total_chapters_expected = meta.total_chapters_expected
    work.hooked = True
    work.hooked_at = _utcnow()
    db.commit()
    db.refresh(work)

    chapter_refs = await adapter.list_chapters(meta)
    existing = {c.index: c for c in work.chapters}
    for cref in chapter_refs:
        if cref.index in existing:
            existing[cref.index].source_chapter_ref = cref.source_chapter_ref
            if cref.title:
                existing[cref.index].title = cref.title
        else:
            db.add(
                Chapter(
                    work_id=work.id,
                    source_chapter_ref=cref.source_chapter_ref,
                    index=cref.index,
                    title=cref.title or f"Chapter {cref.index}",
                    fetch_status="pending",
                )
            )
    work.total_chapters_known = max(len(chapter_refs), len(existing))
    db.commit()

    # Enqueue a slow backfill job (idempotent: reuse an open one if present).
    job = db.scalar(
        select(CrawlJob).where(
            CrawlJob.work_id == work.id,
            CrawlJob.kind == "backfill",
            CrawlJob.status.in_(["scheduled", "running", "paused"]),
        )
    )
    if job is None:
        db.add(
            CrawlJob(
                work_id=work.id,
                kind="backfill",
                status="scheduled",
                scheduled_for=_utcnow(),
                cursor={"next_index": 1},
            )
        )
        db.commit()
    return work


def store_chapter_content(db: Session, chapter: Chapter, raw: RawChapter) -> bool:
    """Sanitize + persist content for a chapter. Returns False if unchanged (deduped)."""
    if raw.fmt in ("text", "txt"):
        html = sanitize_html(text_to_html(raw.body))
    else:
        html = sanitize_html(raw.body)
    checksum = hashlib.sha256(html.encode("utf-8")).hexdigest()

    if chapter.content is not None and chapter.content.checksum == checksum:
        chapter.fetch_status = "fetched"
        chapter.fetched_at = _utcnow()
        db.commit()
        return False

    content = ChapterContent(
        chapter_id=chapter.id,
        format="html",
        body=html,
        word_count=count_words(html),
        checksum=checksum,
    )
    db.add(content)
    db.flush()
    chapter.content_id = content.id
    chapter.content = content
    # Adopt a richer title from the page when ours is empty or a bare "Chapter N".
    if raw.title and (not chapter.title or re.fullmatch(r"Chapter \d+", chapter.title.strip())):
        chapter.title = raw.title
    chapter.fetch_status = "fetched"
    chapter.fetched_at = _utcnow()
    db.commit()
    return True
