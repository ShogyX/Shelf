"""Ingestion engine (Stage 6/7): adapter resolution, compliance gate, hooking."""
from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import Chapter, ChapterContent, CrawlJob, Source, Work
from ..sanitize import count_words, sanitize_html, text_to_html
from .base import RawChapter, SourceAdapter, registry
from .extract import chapter_ref_number
from .fetcher import PoliteFetcher

settings = get_settings()
_fetcher: PoliteFetcher | None = None


class ComplianceError(PermissionError):
    """Raised when the engine refuses to run a non-permitted source."""


def get_fetcher() -> PoliteFetcher:
    global _fetcher
    if _fetcher is None:
        # Total in-flight cap is a generous machine-resource backstop, NOT the crawl-speed knob
        # (per-domain/per-source budgets pace each target). Decoupled from the per-tick batch
        # ("parallel_fetches") so concurrent index/backfill crawls don't compete for slots.
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
    return adapter_cls(get_fetcher(), config=src.config or {})


async def hook_work(db: Session, source_key: str, work_ref: str, *,
                    start_chapter: int = 1) -> Work:
    """Run one polite discovery pass, create Work + pending Chapters, enqueue backfill.

    ``start_chapter`` (1-based) lets the user skip chapters they've already read elsewhere: chapters
    with a lower index are never created or gathered, and the backfill begins there."""
    start_chapter = max(1, start_chapter or 1)
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
    # An adapter that knows its medium (e.g. a comic adapter → comic) wins; never downgrade a
    # comic back to text on a later refresh.
    if meta.media_kind == "comic" or work.media_kind != "comic":
        work.media_kind = meta.media_kind
    if meta.total_chapters_expected and start_chapter <= 1:
        work.total_chapters_expected = meta.total_chapters_expected
    work.start_chapter = start_chapter
    work.hooked = True
    work.hooked_at = _utcnow()
    work.crawl_paused = False  # (re-)hooking means you want it crawled — clear any prior pause
    db.commit()
    db.refresh(work)

    chapter_refs = await adapter.list_chapters(meta)
    existing = {c.index: c for c in work.chapters}

    def _num(title: str | None, ref: str | None, idx: int) -> float:
        return chapter_ref_number(title, ref, idx)

    kept = 0
    first_kept_index: int | None = None
    for cref in chapter_refs:
        # start_chapter is the chapter NUMBER the user wants to begin at — compare against each
        # chapter's real number (from its title/ref), NOT its list position (comix indexes by
        # position; the number is in the title, so position 700 may be 'Chapter 677').
        if _num(cref.title, cref.source_chapter_ref, cref.index) < start_chapter:
            continue  # already read elsewhere — don't create/gather it
        kept += 1
        first_kept_index = cref.index if first_kept_index is None else min(first_kept_index, cref.index)
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
    existing_kept = sum(
        1 for c in existing.values()
        if _num(c.title, c.source_chapter_ref, c.index) >= start_chapter
    )
    work.total_chapters_known = max(kept, existing_kept)
    if start_chapter > 1:
        # Partial hook: the tracked work spans only the kept chapters, so its target IS that count
        # (the tracker raises it as new chapters arrive). Keeping the full-series total here would
        # leave the progress bar stuck below 100% forever.
        work.total_chapters_expected = work.total_chapters_known or None
    elif work.total_chapters_expected and work.total_chapters_known > work.total_chapters_expected:
        # If the source lists more chapters than it advertised, the advertised count is stale —
        # raise the ceiling so we never display "fetched > total".
        work.total_chapters_expected = work.total_chapters_known
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
                cursor={"next_index": first_kept_index or 1},
            )
        )
        db.commit()

    # comix.to serves some page images pre-scrambled; enqueue a descramble job that iterates the
    # captured chapters and repairs them via browser-capture (it re-arms while the backfill runs).
    if src.adapter_key == "comix":
        has_descramble = db.scalar(
            select(CrawlJob).where(
                CrawlJob.work_id == work.id,
                CrawlJob.kind == "descramble",
                CrawlJob.status.in_(["scheduled", "running", "paused"]),
            )
        )
        if has_descramble is None:
            db.add(
                CrawlJob(work_id=work.id, kind="descramble", status="scheduled",
                         scheduled_for=_utcnow(), cursor={})
            )
            db.commit()

    # Pull authoritative metadata (author / synopsis / cover / expected chapter count) from any
    # enabled metadata provider NOW, so a freshly hooked work is enriched immediately rather than
    # waiting up to 6 hours for the next sweep. Strictly best-effort — never breaks the hook.
    try:
        from ..integrations.metadata_sync import enrich_work_all_providers

        await enrich_work_all_providers(db, work)
        db.refresh(work)
    except Exception:  # noqa: BLE001
        db.rollback()
    return work


# Below this visible-word-count an image-less prose page is almost certainly a placeholder /
# "no more chapters" / landing page rather than a real chapter (real chapters run hundreds+).
_MIN_PROSE_WORDS = 50

# Storage outcomes (sequential crawler distinguishes these to know when to stop chaining).
STORED = "stored"        # new content persisted
UNCHANGED = "unchanged"  # identical to this chapter's existing content (re-fetch)
DEAD_END = "dead_end"    # placeholder / duplicate-of-another-chapter → the serial has no more


def _is_dead_end(db: Session, chapter: Chapter, html: str, checksum: str, words: int) -> bool:
    """A synthesized/next-linked page that isn't a real new chapter — the serial's frontier.

    Two robust signals: (1) it's identical to a DIFFERENT already-fetched chapter of this work
    (a 'next' link that loops back, or a generic placeholder served repeatedly); (2) it carries
    almost no text and no images (a 'coming soon' / 'not found' / landing page). The empty-page
    signal is only trusted once the work already has real content, so a genuinely short first
    chapter on a brand-new hook is never mistaken for the end."""
    dupe = db.scalar(
        select(ChapterContent.id)
        .join(Chapter, Chapter.content_id == ChapterContent.id)
        .where(
            Chapter.work_id == chapter.work_id,
            Chapter.id != chapter.id,
            ChapterContent.checksum == checksum,
        )
        .limit(1)
    )
    if dupe is not None:
        return True
    if words < _MIN_PROSE_WORDS and "<img" not in html:
        has_real_content = db.scalar(
            select(func.count(Chapter.id)).where(
                Chapter.work_id == chapter.work_id,
                Chapter.id != chapter.id,
                Chapter.fetch_status == "fetched",
            )
        ) or 0
        return has_real_content > 0
    return False


def store_chapter_content(
    db: Session, chapter: Chapter, raw: RawChapter, *, detect_dead_end: bool = False
) -> str:
    """Sanitize + persist content for a chapter. Returns one of ``STORED`` / ``UNCHANGED`` /
    ``DEAD_END``. With ``detect_dead_end`` (the sequential crawler), a placeholder/duplicate page
    is NOT stored as a real chapter — it's marked ``skipped`` and reported as ``DEAD_END`` so the
    crawl stops chaining instead of growing the work forever.

    On any failure the session is rolled back so a half-applied flush can't poison the
    caller's subsequent commit (the scheduler records the error on the same session)."""
    try:
        return _store_chapter_content(db, chapter, raw, detect_dead_end)
    except Exception:
        db.rollback()
        raise


def _store_chapter_content(
    db: Session, chapter: Chapter, raw: RawChapter, detect_dead_end: bool = False
) -> str:
    if raw.fmt in ("text", "txt"):
        html = sanitize_html(text_to_html(raw.body))
    else:
        html = sanitize_html(raw.body)
    # Download every remote image (comic pages / illustrations) to a permanent local copy
    # and rewrite the src, so reading never hits the network (and short-lived token image
    # URLs don't expire). No-op for prose chapters with no remote <img>.
    from .. import imagecache
    html = imagecache.localize_html_images(html)
    checksum = hashlib.sha256(html.encode("utf-8")).hexdigest()

    if chapter.content is not None and chapter.content.checksum == checksum:
        chapter.fetch_status = "fetched"
        chapter.fetched_at = _utcnow()
        db.commit()
        return UNCHANGED

    words = count_words(html)
    if detect_dead_end and _is_dead_end(db, chapter, html, checksum, words):
        # Don't persist the placeholder as content; mark the chapter a dead-end so the backfill
        # finalizes (caught up) and the refresh re-probes this same URL later (the real chapter
        # may publish there). Never counted as fetched, so it doesn't inflate the library.
        chapter.fetch_status = "skipped"
        chapter.fetched_at = _utcnow()
        db.commit()
        return DEAD_END

    content = ChapterContent(
        chapter_id=chapter.id,
        format="html",
        body=html,
        word_count=words,
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
    return STORED
