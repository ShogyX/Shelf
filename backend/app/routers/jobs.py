from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import current_user, require_admin, require_permission
from ..db import get_db
from ..ingestion.base import registry
from ..ingestion.engine import ComplianceError, ensure_source, hook_work
from ..ingestion.local_folder import upsert_media_work
from ..ingestion.media import is_supported, parse_media
from ..library import add_to_library, validate_shelf
from ..models import CrawlJob, User, Work
from ..schemas import HookIn, JobOut, WorkOut
from .works import apply_crawl_policy

router = APIRouter()


def _utcnow() -> datetime:
    return datetime.now(UTC)


@router.get("/jobs", response_model=list[JobOut], dependencies=[Depends(require_permission("jobs.view"))])
def list_jobs(
    db: Session = Depends(get_db),
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> list[CrawlJob]:
    # Bounded: the Jobs page polls this every ~4s; returning the WHOLE crawl_jobs table serialized
    # all history on a large library each poll. Newest-first, capped (default 200).
    return list(db.scalars(
        select(CrawlJob).order_by(CrawlJob.created_at.desc()).limit(limit).offset(offset)
    ).all())


@router.post("/jobs/reap", dependencies=[Depends(require_admin)])
def reap_jobs() -> dict:
    """Manually run the stalled-job reaper (also runs automatically on a timer)."""
    from ..ingestion.scheduler import reap_stalled_jobs

    return {"revived": reap_stalled_jobs()}


def _set_crawl_paused(db: Session, work_id: int, paused: bool) -> None:
    """Toggle a work's crawl_paused flag — the gate that stops the reaper/refresh from
    auto-recreating a job the operator deleted/paused (and lets resume/retry re-enable it)."""
    work = db.get(Work, work_id)
    if work is not None:
        work.crawl_paused = paused


@router.post("/jobs/{job_id}/pause", response_model=JobOut, dependencies=[Depends(require_admin)])
def pause_job(job_id: int, db: Session = Depends(get_db)) -> CrawlJob:
    job = db.get(CrawlJob, job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    if job.status in ("scheduled", "running"):
        job.status = "paused"
    # Pause sticks: stop the reaper from re-arming this work until the operator resumes.
    _set_crawl_paused(db, job.work_id, True)
    db.commit()
    db.refresh(job)
    return job


@router.post("/jobs/{job_id}/resume", response_model=JobOut, dependencies=[Depends(require_admin)])
def resume_job(job_id: int, db: Session = Depends(get_db)) -> CrawlJob:
    job = db.get(CrawlJob, job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    if job.status == "paused":
        job.status = "scheduled"
        job.scheduled_for = _utcnow()
    _set_crawl_paused(db, job.work_id, False)  # re-enable auto-scheduling
    db.commit()
    db.refresh(job)
    return job


@router.post("/jobs/{job_id}/retry", response_model=JobOut, dependencies=[Depends(require_admin)])
def retry_job(job_id: int, db: Session = Depends(get_db)) -> CrawlJob:
    """Renew a stalled/errored/finished job: re-queue the work's failed chapters and
    re-arm the job to run now (clears the error)."""
    from sqlalchemy import update

    from ..models import Chapter

    job = db.get(CrawlJob, job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    db.execute(
        update(Chapter)
        .where(Chapter.work_id == job.work_id, Chapter.fetch_status == "failed")
        .values(fetch_status="pending")
    )
    job.status = "scheduled"
    job.scheduled_for = _utcnow()
    job.last_error = None
    job.attempts = 0
    job.finished_at = None
    _set_crawl_paused(db, job.work_id, False)  # renewing resumes auto-scheduling
    db.commit()
    db.refresh(job)
    return job


@router.delete("/jobs/{job_id}", dependencies=[Depends(require_admin)])
def delete_job(job_id: int, db: Session = Depends(get_db)) -> dict:
    """Delete a crawl-job and STOP the work's crawl: the work is marked crawl_paused so the
    reaper / refresh scheduler won't auto-recreate the job (that resurrection is the bug being
    fixed). Gathered chapters are kept. Resume later via the job's Renew/Resume, or 'Check for
    updates' on the work."""
    job = db.get(CrawlJob, job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    _set_crawl_paused(db, job.work_id, True)
    db.delete(job)
    db.commit()
    return {"deleted": job_id}


@router.post("/works/hook", response_model=WorkOut, dependencies=[Depends(require_permission("add.use"))])
async def hook(
    payload: HookIn, user: User = Depends(current_user), db: Session = Depends(get_db)
) -> Work:
    try:
        work = await hook_work(db, payload.source_key, payload.work_ref)
    except ComplianceError as exc:
        raise HTTPException(403, str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"Failed to hook work: {exc}") from exc
    shelf_id = validate_shelf(db, user.id, payload.shelf_id)
    # Add it to THIS user's library (the Work + crawl are shared; membership is per-user).
    add_to_library(db, user.id, work.id, shelf_id=shelf_id)
    # Apply any per-title crawl policy chosen at hook time.
    if any(getattr(payload, a) is not None for a in
           ("crawl_interval_s", "crawl_window_start", "crawl_window_end")):
        apply_crawl_policy(work, payload)
        db.commit()
        db.refresh(work)
    return work


@router.post("/works/{work_id}/unhook", response_model=WorkOut,
             dependencies=[Depends(require_admin)])
def unhook(work_id: int, db: Session = Depends(get_db)) -> Work:
    # Globally unhooks the SHARED work + pauses its crawl jobs for everyone → admin only.
    work = db.get(Work, work_id)
    if work is None:
        raise HTTPException(404, "Work not found")
    work.hooked = False
    for job in db.scalars(
        select(CrawlJob).where(
            CrawlJob.work_id == work_id, CrawlJob.status.in_(["scheduled", "running", "paused"])
        )
    ).all():
        job.status = "paused"
    db.commit()
    db.refresh(work)
    return work


@router.post("/works/import", response_model=WorkOut, dependencies=[Depends(require_permission("add.use"))])
async def import_file(
    file: UploadFile = File(...),
    shelf_id: int | None = Form(None),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Work:
    """Local import: upload an EPUB / TXT / MD / PDF / CBZ / CBR file you own."""
    filename = file.filename or "uploaded"
    shelf_id = validate_shelf(db, user.id, shelf_id)
    if not is_supported(filename):
        raise HTTPException(415, "Unsupported file type (EPUB/TXT/MD/PDF/CBZ/CBR only).")
    data = await file.read()
    src = ensure_source(db, registry.get("local_import"))
    try:
        parsed = parse_media(data, filename)
    except Exception as exc:
        raise HTTPException(422, f"Could not read file: {exc}") from exc

    work = upsert_media_work(
        db, src,
        source_work_ref=f"local:{filename}",
        parsed=parsed,
        cover_key=f"local-{filename}",
    )
    add_to_library(db, user.id, work.id, shelf_id=shelf_id)
    return work
