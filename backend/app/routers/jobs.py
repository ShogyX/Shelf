from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..ingestion.base import registry
from ..ingestion.engine import ComplianceError, ensure_source, hook_work
from ..ingestion.local_folder import upsert_media_work
from ..ingestion.media import is_supported, parse_media
from ..models import CrawlJob, Work
from ..schemas import HookIn, JobOut, WorkOut
from .works import apply_crawl_policy

router = APIRouter()


def _utcnow() -> datetime:
    return datetime.now(UTC)


@router.get("/jobs", response_model=list[JobOut])
def list_jobs(db: Session = Depends(get_db)) -> list[CrawlJob]:
    return list(db.scalars(select(CrawlJob).order_by(CrawlJob.created_at.desc())).all())


@router.post("/jobs/reap")
def reap_jobs() -> dict:
    """Manually run the stalled-job reaper (also runs automatically on a timer)."""
    from ..ingestion.scheduler import reap_stalled_jobs

    return {"revived": reap_stalled_jobs()}


@router.post("/jobs/{job_id}/pause", response_model=JobOut)
def pause_job(job_id: int, db: Session = Depends(get_db)) -> CrawlJob:
    job = db.get(CrawlJob, job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    if job.status in ("scheduled", "running"):
        job.status = "paused"
        db.commit()
        db.refresh(job)
    return job


@router.post("/jobs/{job_id}/resume", response_model=JobOut)
def resume_job(job_id: int, db: Session = Depends(get_db)) -> CrawlJob:
    job = db.get(CrawlJob, job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    if job.status == "paused":
        job.status = "scheduled"
        job.scheduled_for = _utcnow()
        db.commit()
        db.refresh(job)
    return job


@router.post("/works/hook", response_model=WorkOut)
async def hook(payload: HookIn, db: Session = Depends(get_db)) -> Work:
    try:
        work = await hook_work(db, payload.source_key, payload.work_ref)
    except ComplianceError as exc:
        raise HTTPException(403, str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"Failed to hook work: {exc}") from exc
    # Apply any per-title crawl policy chosen at hook time.
    if any(getattr(payload, a) is not None for a in
           ("crawl_interval_s", "crawl_daily_limit", "crawl_window_start", "crawl_window_end")):
        apply_crawl_policy(work, payload)
        db.commit()
        db.refresh(work)
    return work


@router.post("/works/{work_id}/unhook", response_model=WorkOut)
def unhook(work_id: int, db: Session = Depends(get_db)) -> Work:
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


@router.post("/works/import", response_model=WorkOut)
async def import_file(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> Work:
    """Local import: upload an EPUB / TXT / MD / PDF / CBZ / CBR file you own."""
    filename = file.filename or "uploaded"
    if not is_supported(filename):
        raise HTTPException(415, "Unsupported file type (EPUB/TXT/MD/PDF/CBZ/CBR only).")
    data = await file.read()
    src = ensure_source(db, registry.get("local_import"))
    try:
        parsed = parse_media(data, filename)
    except Exception as exc:
        raise HTTPException(422, f"Could not read file: {exc}") from exc

    return upsert_media_work(
        db, src,
        source_work_ref=f"local:{filename}",
        parsed=parsed,
        cover_key=f"local-{filename}",
    )
