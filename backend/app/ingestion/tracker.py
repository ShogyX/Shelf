"""Update tracker — keep hooked titles current with their source.

Serialized fiction gains chapters over time. For each hooked title this module:
  * re-discovers it at the source and refreshes metadata (cover, synopsis, author,
    advertised chapter total, ongoing/complete status);
  * finds newly-available chapters and enqueues them so the scheduler ingests them —
    both for enumerable-TOC works AND for sequential "next-link" serials (where the
    table of contents only ever yields the seed, so we re-seed from the last chapter).

`discover_updates` is the shared core used by both the on-demand API and the periodic
refresh job. Static sources (Gutenberg/Standard Ebooks/local files) never gain remote
chapters and are skipped.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Chapter, CrawlJob, Work
from .base import ChapterRef, WorkMeta
from .engine import adapter_for
from .extract import synthesize_next_chapter_url

log = logging.getLogger("shelf.tracker")

# Adapters whose works are fixed once imported (no new remote chapters to track).
STATIC_SOURCES = {"gutenberg", "standardebooks", "local_import", "local_folder"}


def _utcnow() -> datetime:
    return datetime.now(UTC)


def is_trackable(work: Work) -> bool:
    """A hooked work with a remote, non-static source can gain new content."""
    return bool(
        work.hooked
        and work.source is not None
        and work.source.adapter_key not in STATIC_SOURCES
        and work.source_work_ref
    )


def _meta_from_work(work: Work) -> WorkMeta:
    return WorkMeta(
        source_work_ref=work.source_work_ref or "",
        title=work.title,
        author=work.author,
        description=work.description,
        cover_url=work.cover_url,
        language=work.language or "en",
        status=work.status,
        total_chapters_expected=work.total_chapters_expected,
    )


def _apply_metadata(work: Work, meta: WorkMeta) -> bool:
    """Refresh metadata from a fresh discovery. Upgrade-only (never blanks a field)."""
    changed = False
    if meta.author and meta.author != work.author:
        work.author = meta.author
        changed = True
    if meta.description and meta.description != work.description:
        work.description = meta.description
        changed = True
    if meta.cover_url and meta.cover_url != work.cover_url:
        work.cover_url = meta.cover_url
        changed = True
    if meta.total_chapters_expected:
        new_total = max(meta.total_chapters_expected, work.total_chapters_expected or 0)
        if new_total != work.total_chapters_expected:
            work.total_chapters_expected = new_total
            changed = True
    if meta.status and meta.status != work.status:
        work.status = meta.status
        changed = True
    return changed


async def _reseed_sequential(db: Session, work: Work, adapter, existing_refs: set[str]) -> int:
    """A sequential serial's TOC only yields the seed, so continue from the top chapter:
    synthesize the next numeric URL (cheap) or scrape the 'next' link off the last page."""
    top = db.scalar(
        select(Chapter).where(Chapter.work_id == work.id)
        .order_by(Chapter.index.desc()).limit(1)
    )
    if top is None or not top.source_chapter_ref:
        return 0
    nxt = synthesize_next_chapter_url(top.source_chapter_ref)
    if nxt is None:
        try:
            raw = await adapter.fetch_chapter(ChapterRef(
                source_chapter_ref=top.source_chapter_ref, index=top.index, title=top.title or "",
            ))
            nxt = raw.next_ref
        except Exception:  # noqa: BLE001
            nxt = None
    if not nxt or nxt in existing_refs:
        return 0
    db.add(Chapter(
        work_id=work.id, source_chapter_ref=nxt, index=top.index + 1,
        title=f"Chapter {top.index + 1}", fetch_status="pending",
    ))
    return 1


async def _discover_new_chapters(db: Session, work: Work, adapter, meta: WorkMeta) -> int:
    """Enqueue chapters the source now offers that we don't have yet."""
    existing_idx = {c.index for c in work.chapters}
    existing_refs = {c.source_chapter_ref for c in work.chapters if c.source_chapter_ref}
    added = 0
    try:
        refs = await adapter.list_chapters(meta)
    except Exception as exc:  # noqa: BLE001
        log.info("tracker list_chapters failed work=%s: %s", work.id, exc)
        refs = []
    for cref in refs:
        if cref.index in existing_idx or cref.source_chapter_ref in existing_refs:
            continue
        db.add(Chapter(
            work_id=work.id, source_chapter_ref=cref.source_chapter_ref, index=cref.index,
            title=cref.title or f"Chapter {cref.index}", fetch_status="pending",
        ))
        existing_idx.add(cref.index)
        existing_refs.add(cref.source_chapter_ref)
        added += 1
    # Sequential serials: the TOC didn't reveal anything new → re-seed from the top.
    if added == 0:
        added += await _reseed_sequential(db, work, adapter, existing_refs)
    return added


def _ensure_refresh_job(db: Session, work: Work) -> None:
    open_job = db.scalar(
        select(CrawlJob).where(
            CrawlJob.work_id == work.id,
            CrawlJob.status.in_(["scheduled", "running", "paused"]),
        )
    )
    if open_job is not None:
        if open_job.status == "paused":
            open_job.status = "scheduled"
            open_job.scheduled_for = _utcnow()
        return
    db.add(CrawlJob(work_id=work.id, kind="refresh", status="scheduled",
                    scheduled_for=_utcnow(), cursor={}))


async def discover_updates(db: Session, work: Work, adapter) -> tuple[int, bool]:
    """Shared core: refresh metadata + enqueue new chapters. Returns (added, meta_changed).
    Does NOT stamp timestamps or commit — callers decide that."""
    meta = await adapter.discover_work(work.source_work_ref or "")
    changed = _apply_metadata(work, meta)
    added = await _discover_new_chapters(db, work, adapter, meta)
    if added:
        total = db.scalar(
            select(func.count(Chapter.id)).where(Chapter.work_id == work.id)
        ) or 0
        work.total_chapters_known = max(work.total_chapters_known, total)
    return added, changed


async def check_work(db: Session, work: Work) -> dict:
    """On-demand: re-check one hooked title now. Stamps last_checked_at / last_update_at."""
    result = {
        "work_id": work.id, "checked": True, "new_chapters": 0,
        "metadata_changed": False, "status": work.status,
        "total_chapters_expected": work.total_chapters_expected, "error": None,
    }
    if not is_trackable(work):
        work.last_checked_at = _utcnow()
        db.commit()
        result["checked"] = False
        return result
    try:
        adapter = adapter_for(work.source)  # raises if disabled / not permitted
        added, changed = await discover_updates(db, work, adapter)
    except Exception as exc:  # noqa: BLE001
        work.last_checked_at = _utcnow()
        db.commit()
        result["error"] = str(exc) or type(exc).__name__
        return result

    if added:
        _ensure_refresh_job(db, work)
    now = _utcnow()
    work.last_checked_at = now
    if added or changed:
        work.last_update_at = now
    db.commit()
    db.refresh(work)
    result.update(
        new_chapters=added, metadata_changed=changed, status=work.status,
        total_chapters_expected=work.total_chapters_expected,
    )
    log.info("tracker work=%s new=%s meta_changed=%s", work.id, added, changed)
    return result


async def check_all(db: Session) -> dict:
    """Re-check every trackable hooked title (politely; the fetcher throttles per source)."""
    works = db.scalars(select(Work).where(Work.hooked.is_(True))).all()
    checked = updated = total_new = 0
    for work in works:
        if not is_trackable(work):
            continue
        r = await check_work(db, work)
        if r["checked"]:
            checked += 1
        total_new += r["new_chapters"]
        if r["new_chapters"] or r["metadata_changed"]:
            updated += 1
    return {"works_checked": checked, "works_updated": updated, "new_chapters": total_new}
