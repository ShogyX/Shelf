"""Self-diagnostics for hooked works.

Two jobs the app does on its own so a user isn't left with a half-pulled book:

1. ``completeness`` — does what we gathered match what the source advertises? Detects
   missing-chapter gaps, failed chapters, and a stalled sequential crawl, and writes a
   ``health`` verdict onto the Work.
2. ``repair`` — try to FIX the gaps: reset failed chapters to retry, synthesize URLs for
   missing numeric chapters, and re-seed a stalled sequential crawl so the scheduler
   keeps going. Pure-DB and idempotent (safe to run repeatedly).

``troubleshoot_discovery`` handles the "we think we found a title but no chapters turned
up" case at hook time by probing a few likely first-chapter URLs before giving up.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime

from bs4 import BeautifulSoup
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Chapter, CrawlJob, Work
from .extract import (
    chapter_num_from_ref,
    chapter_number,
    series_prefix,
    synthesize_next_chapter_url,
)

log = logging.getLogger("shelf.diagnose")

# Below this many chars of body text a probed page is treated as "not a real chapter".
_MIN_CHAPTER_CHARS = 200


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _counts(db: Session, work: Work) -> dict:
    rows = dict(
        db.execute(
            select(Chapter.fetch_status, func.count(Chapter.id))
            .where(Chapter.work_id == work.id)
            .group_by(Chapter.fetch_status)
        ).all()
    )
    fetched = rows.get("fetched", 0)
    failed = rows.get("failed", 0)
    pending = rows.get("pending", 0)
    unavailable = rows.get("unavailable", 0)  # members-only / paywalled — terminal, not a fault
    listed = sum(rows.values())
    max_index = db.scalar(
        select(func.max(Chapter.index)).where(Chapter.work_id == work.id)
    ) or 0
    return {
        "fetched": fetched, "failed": failed, "pending": pending,
        "unavailable": unavailable, "listed": listed, "max_index": max_index,
    }


def _gaps(db: Session, work: Work, max_index: int) -> list[int]:
    """Index holes WITHIN the span of existing rows (true missing-chapter holes).

    Scans from the minimum present index, not from 1: a work hooked from a later chapter
    (``start_chapter`` > 1) legitimately keeps the source's POSITION indices, which start well
    above 1 (e.g. comix position 723 = 'Chapter 700'). Ranging from 1 would mis-read that whole
    offset as missing chapters and trigger a synthesize-everything 'repair'."""
    if max_index <= 0:
        return []
    have = {
        i for (i,) in db.execute(
            select(Chapter.index).where(Chapter.work_id == work.id)
        ).all()
    }
    if not have:
        return []
    return [i for i in range(min(have), max_index + 1) if i not in have]


def _num(ref: str | None, title: str | None) -> int | None:
    """Integer chapter number from a chapter's URL ref (preferred, anchored to the chapter
    token so a slug number doesn't fool it) or its title."""
    if ref:
        n = chapter_num_from_ref(ref)
        if n is not None:
            return n
    t = chapter_number(title or "")
    return int(t) if (t is not None and float(t).is_integer()) else None


def numeric_gaps(db: Session, work: Work) -> list[int]:
    """Missing chapter NUMBERS within a dense numeric run — a *skipped* chapter.

    Sequential crawling assigns contiguous `index` values (max+1), so a next-link that
    jumps 4→6 leaves no index hole; the skip only shows in the chapter numbers parsed from
    the URLs/titles. This is conservative: it only flags works whose chapters are almost
    entirely one integer run (so irregular numbering / side-stories aren't mistaken for
    gaps), and only when a handful are missing."""
    rows = db.execute(
        select(Chapter.source_chapter_ref, Chapter.title).where(Chapter.work_id == work.id)
    ).all()
    total = len(rows)
    numbered: set[int] = set()
    for ref, title in rows:
        n = _num(ref, title)
        if n is not None:
            numbered.add(n)
    # Must be almost entirely a numbered run (allow a couple of unnumbered front-matter
    # chapters), so irregular numbering / side-stories aren't mistaken for a sequence.
    if total < 3 or not numbered or (total - len(numbered)) > max(2, int(total * 0.15)):
        return []
    missing = [i for i in range(min(numbered), max(numbered) + 1) if i not in numbered]
    # A genuine "skipping single chapters" is a few holes relative to what's present — not
    # a huge span (which would mean the numbers just aren't a 1..N sequence).
    if not missing or len(missing) > max(3, int(len(numbered) * 0.25)):
        return []
    return missing


def _reindex_by_number(db: Session, work: Work) -> None:
    """Order chapter indices by chapter number so filled-in gaps read in sequence.
    Unnumbered chapters (prologue/front-matter) keep their place right after whatever
    numbered chapter they currently follow."""
    chs = db.scalars(
        select(Chapter).where(Chapter.work_id == work.id).order_by(Chapter.index)
    ).all()
    keyed = []
    last = 0.0
    for pos, ch in enumerate(chs):
        n = _num(ch.source_chapter_ref, ch.title)
        if n is not None:
            last = float(n)
            keyed.append(((float(n), 0, pos), ch))
        else:
            keyed.append(((last, 1, pos), ch))  # stays just after its preceding numbered ch
    keyed.sort(key=lambda t: t[0])
    ordered = [ch for _k, ch in keyed]
    # Two passes (negative temporaries) so the (work_id, index) unique constraint never
    # collides mid-reassignment.
    for i, ch in enumerate(ordered, start=1):
        ch.index = -i
    db.flush()
    for i, ch in enumerate(ordered, start=1):
        ch.index = i
    db.flush()


def repair_numeric_gaps(db: Session, work: Work) -> int:
    """Insert pending rows for skipped chapter numbers (URLs synthesized from the series
    prefix) and re-sequence indices so they read in order. Returns how many were added."""
    missing = numeric_gaps(db, work)
    if not missing:
        return 0
    prefix = _chapter_url_prefix(db, work)
    if not prefix:
        return 0  # can't synthesize the missing chapter's URL → nothing fetchable to add
    existing = {
        r for (r,) in db.execute(
            select(Chapter.source_chapter_ref).where(Chapter.work_id == work.id)
        ).all()
    }
    top = db.scalar(select(func.max(Chapter.index)).where(Chapter.work_id == work.id)) or 0
    added = 0
    for n in missing:
        ref = f"{prefix}{n}"
        if ref in existing:
            continue
        top += 1
        db.add(Chapter(
            work_id=work.id, source_chapter_ref=ref, index=top,
            title=f"Chapter {n}", fetch_status="pending",
        ))
        added += 1
    if added:
        db.flush()
        _reindex_by_number(db, work)
        cnt = db.scalar(select(func.count(Chapter.id)).where(Chapter.work_id == work.id)) or 0
        work.total_chapters_known = max(work.total_chapters_known or 0, cnt)
    return added


def completeness(db: Session, work: Work) -> dict:
    """Diagnose how complete a work is. Returns a structured report and a health verdict.

    health: ok | incomplete | no_chapters | unknown
    """
    c = _counts(db, work)
    advertised = work.total_chapters_expected
    gaps = _gaps(db, work, c["max_index"])
    ngaps = numeric_gaps(db, work)  # skipped chapter NUMBERS (contiguous-index works)
    has_open_job = db.scalar(
        select(CrawlJob.id).where(
            CrawlJob.work_id == work.id,
            CrawlJob.status.in_(["scheduled", "running", "paused"]),
        )
    ) is not None

    if c["listed"] == 0:
        health = "no_chapters"
        detail = "No chapters were discovered for this title."
    elif c["fetched"] == 0 and c.get("unavailable") and not has_open_job:
        # Everything that could be listed is members-only/paywalled — a terminal, explainable
        # state (not a crawl fault). Don't keep flagging it as broken.
        health = "incomplete"
        detail = (
            f"{c['unavailable']} chapter(s) are members-only — sign in to the source "
            "(set its access token) to download them."
        )
    elif c["fetched"] == 0 and not has_open_job:
        health = "no_chapters"
        detail = "Chapters were listed but none could be fetched."
    else:
        missing_vs_advertised = bool(advertised and c["fetched"] < advertised)
        # A still-running backfill isn't "incomplete" — it's just in progress.
        incomplete = (gaps or ngaps or c["failed"] or missing_vs_advertised) and not (
            has_open_job and c["pending"]
        )
        if incomplete:
            health = "incomplete"
            parts = []
            if gaps:
                parts.append(f"{len(gaps)} missing chapter(s)")
            if ngaps:
                parts.append(f"{len(ngaps)} skipped chapter(s)")
            if c["failed"]:
                parts.append(f"{c['failed']} failed to fetch")
            if missing_vs_advertised:
                parts.append(f"{c['fetched']}/{advertised} fetched vs. advertised")
            detail = "; ".join(parts) or "Some chapters are missing."
        elif has_open_job and c["pending"]:
            health = "ok"
            detail = "Gathering in progress."
        else:
            health = "ok"
            detail = "All discovered chapters fetched."

    return {
        "health": health,
        "detail": detail,
        "fetched": c["fetched"],
        "failed": c["failed"],
        "pending": c["pending"],
        "listed": c["listed"],
        "advertised": advertised,
        "max_index": c["max_index"],
        "gaps": gaps,
        "chapter_gaps": ngaps,
        "has_open_job": has_open_job,
    }


def apply_health(db: Session, work: Work, report: dict) -> None:
    work.health = report["health"]
    work.health_detail = (report.get("detail") or "")[:1000] or None
    work.health_checked_at = _utcnow()
    # Keep the advertised ceiling honest: a serial that gathered chapters past its old total
    # should report the higher number, not "fetched > total". This also heals works that drifted
    # into that state before the discovery paths maintained the invariant. `listed` is the true
    # row count; never let the displayed total sit below it.
    listed = report.get("listed") or 0
    if work.total_chapters_known < listed:
        work.total_chapters_known = listed
    if work.total_chapters_expected and listed > work.total_chapters_expected:
        work.total_chapters_expected = listed
    db.commit()


def _ensure_backfill_job(db: Session, work: Work) -> bool:
    """Make sure there's an open backfill job so the scheduler picks pending chapters up."""
    existing = db.scalar(
        select(CrawlJob).where(
            CrawlJob.work_id == work.id,
            CrawlJob.status.in_(["scheduled", "running", "paused"]),
        )
    )
    if existing:
        # This is only ever called after queueing new work (a re-added gap chapter), so
        # pull a parked/paused job forward to run at the NEXT tick — otherwise the re-add
        # would wait for the head crawl's next scheduled run (which can be far off under a
        # per-title interval). The pending query is ordered by index, so the low-index gap
        # is fetched first when that tick runs.
        if existing.status in ("scheduled", "paused"):
            existing.status = "scheduled"
            existing.scheduled_for = _utcnow()
            db.commit()
        return False
    db.add(CrawlJob(work_id=work.id, kind="backfill", status="scheduled",
                    scheduled_for=_utcnow(), cursor={"next_index": 1}))
    db.commit()
    return True


def _chapter_url_prefix(db: Session, work: Work) -> str | None:
    """A numeric chapter-URL prefix shared by this work's chapters, if any
    (lets us synthesize the URL of a missing/next numeric chapter)."""
    refs = [
        r for (r,) in db.execute(
            select(Chapter.source_chapter_ref).where(Chapter.work_id == work.id)
        ).all() if r
    ]
    for ref in refs:
        p = series_prefix(ref)
        if p:
            return p
    return None


def repair(db: Session, work: Work) -> dict:
    """Attempt to fix an incomplete work. Returns the actions taken + a fresh report.

    Idempotent: retries failed chapters, synthesizes URLs for missing numeric chapters,
    re-seeds a stalled sequential crawl, and (re)opens a backfill job."""
    actions: list[str] = []
    before = completeness(db, work)
    prefix = _chapter_url_prefix(db, work)

    # 1. Retry chapters that previously failed.
    failed = db.scalars(
        select(Chapter).where(Chapter.work_id == work.id, Chapter.fetch_status == "failed")
    ).all()
    for ch in failed:
        ch.fetch_status = "pending"
        if not ch.source_chapter_ref and prefix:
            ch.source_chapter_ref = f"{prefix}{ch.index}"
    if failed:
        actions.append(f"retry {len(failed)} failed chapter(s)")

    # 2. Fill true holes (missing index rows) when we can synthesize their URL.
    if prefix:
        for i in before["gaps"]:
            db.add(Chapter(
                work_id=work.id, source_chapter_ref=f"{prefix}{i}", index=i,
                title=f"Chapter {i}", fetch_status="pending",
            ))
        if before["gaps"]:
            actions.append(f"enqueue {len(before['gaps'])} missing chapter(s)")
            work.total_chapters_known = max(work.total_chapters_known, before["max_index"])

    # 2b. Fill SKIPPED chapter numbers (dense numeric run missing a number, even though
    #     the index column is contiguous — the classic sequential-crawl single-skip).
    n_added = repair_numeric_gaps(db, work)
    if n_added:
        actions.append(f"fill {n_added} skipped chapter(s)")

    # 3. Re-seed a stalled sequential crawl: advertised says more, but the head stopped
    #    and nothing is pending. Synthesize the next chapter after the highest fetched.
    db.commit()
    mid = completeness(db, work)
    if (
        work.status != "complete"
        and mid["advertised"] and mid["fetched"] < mid["advertised"]
        and mid["pending"] == 0 and mid["max_index"] > 0
    ):
        top = db.scalar(
            select(Chapter).where(Chapter.work_id == work.id)
            .order_by(Chapter.index.desc()).limit(1)
        )
        # Only extend from a genuinely fetched frontier. If the tail is a 'skipped' dead-end we
        # already probed (placeholder/duplicate), re-seeding would invent ever-higher phantom
        # chapters every integrity tick — climbing forever past the real end.
        if top is not None and top.fetch_status != "fetched":
            top = None
        nxt = synthesize_next_chapter_url(top.source_chapter_ref or "") if top else None
        already = nxt and db.scalar(
            select(Chapter.id).where(
                Chapter.work_id == work.id, Chapter.source_chapter_ref == nxt
            )
        )
        if nxt and not already:
            db.add(Chapter(
                work_id=work.id, source_chapter_ref=nxt, index=top.index + 1,
                title=f"Chapter {top.index + 1}", fetch_status="pending",
            ))
            work.total_chapters_known = max(work.total_chapters_known, top.index + 1)
            actions.append("re-seed stalled sequential crawl")
            db.commit()

    if _ensure_backfill_job(db, work):
        actions.append("reopen backfill job")

    report = completeness(db, work)
    apply_health(db, work, report)
    report["actions"] = actions
    log.info("repair work=%s actions=%s -> %s", work.id, actions, report["health"])
    return report


# Likely first-chapter URL shapes for a sequential seed when discovery found nothing.
def _seed_candidates(work_url: str) -> list[str]:
    base = work_url.rstrip("/")
    return [
        f"{base}/chapter/1", f"{base}/chapter-1", f"{base}/chapter/1/",
        f"{base}/chapter-1/", f"{base}/1", f"{base}/ch-1", f"{base}/episode-1",
    ]


async def troubleshoot_discovery(db: Session, work: Work, adapter, work_url: str) -> dict:
    """When a hooked work has NO chapters, probe likely first-chapter URLs and seed one
    so the scheduler's sequential crawl can take over. Returns what was tried."""
    if db.scalar(select(func.count(Chapter.id)).where(Chapter.work_id == work.id)):
        return {"seeded": False, "tried": [], "reason": "already has chapters"}

    tried: list[str] = []
    for cand in _seed_candidates(work_url):
        tried.append(cand)
        try:
            resp = await adapter.fetcher.get_html(adapter.key, cand)
            resp.raise_for_status()
            body_len = len(BeautifulSoup(resp.text, "lxml").get_text(" ", strip=True))
        except Exception as exc:  # noqa: BLE001
            log.info("seed probe failed %s: %s", cand, exc)
            continue
        if body_len >= _MIN_CHAPTER_CHARS:
            db.add(Chapter(
                work_id=work.id, source_chapter_ref=cand, index=1,
                title="Chapter 1", fetch_status="pending",
            ))
            work.total_chapters_known = max(work.total_chapters_known, 1)
            db.commit()
            _ensure_backfill_job(db, work)
            return {"seeded": True, "tried": tried, "seed_url": cand}
    return {"seeded": False, "tried": tried, "reason": "no first chapter found"}
