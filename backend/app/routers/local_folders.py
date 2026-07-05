"""Watched local-folder management API."""
from __future__ import annotations

import os

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..ingestion.local_folder import sync_folder
from ..library import purge_work
from ..ingestion.watcher import manager
from ..models import Source, WatchedFolder, Work
from ..schemas import WatchedFolderIn, WatchedFolderOut

router = APIRouter()


def _local_source_id(db: Session) -> int | None:
    return db.scalar(select(Source.id).where(Source.key == "local_folder"))


def _to_out(db: Session, folder: WatchedFolder) -> WatchedFolderOut:
    src_id = _local_source_id(db)
    works = 0
    if src_id is not None:
        works = (
            db.scalar(
                select(func.count(Work.id)).where(
                    Work.source_id == src_id,
                    Work.source_work_ref.like(f"localfolder:{folder.id}:%"),
                )
            )
            or 0
        )
    return WatchedFolderOut(
        id=folder.id,
        path=folder.path,
        display_name=folder.display_name,
        recursive=folder.recursive,
        enabled=folder.enabled,
        file_count=folder.file_count,
        works=works,
        last_scan_at=folder.last_scan_at,
        last_error=folder.last_error,
    )


@router.get("/local-folders", response_model=list[WatchedFolderOut])
def list_folders(db: Session = Depends(get_db)) -> list[WatchedFolderOut]:
    folders = db.scalars(select(WatchedFolder).order_by(WatchedFolder.created_at.desc())).all()
    return [_to_out(db, f) for f in folders]


@router.post("/local-folders", response_model=WatchedFolderOut)
def add_folder(payload: WatchedFolderIn, db: Session = Depends(get_db)) -> WatchedFolderOut:
    path = os.path.abspath(os.path.expanduser(payload.path.strip()))
    if not os.path.isdir(path):
        raise HTTPException(400, f"Not a directory: {path}")
    if db.scalar(select(WatchedFolder).where(WatchedFolder.path == path)):
        raise HTTPException(409, "This folder is already mapped.")
    folder = WatchedFolder(
        path=path,
        display_name=payload.display_name or os.path.basename(path) or path,
        recursive=payload.recursive,
        enabled=True,
    )
    db.add(folder)
    db.commit()
    db.refresh(folder)
    # Import what's already there, then start watching for changes.
    sync_folder(db, folder)
    manager.add(folder.id, folder.path, folder.recursive)
    db.refresh(folder)
    return _to_out(db, folder)


@router.post("/local-folders/{folder_id}/rescan", response_model=WatchedFolderOut)
def rescan_folder(folder_id: int, db: Session = Depends(get_db)) -> WatchedFolderOut:
    folder = db.get(WatchedFolder, folder_id)
    if folder is None:
        raise HTTPException(404, "Folder not found")
    sync_folder(db, folder)
    db.refresh(folder)
    return _to_out(db, folder)


@router.delete("/local-folders/{folder_id}")
def delete_folder(
    folder_id: int, remove_works: bool = True, db: Session = Depends(get_db)
) -> dict:
    folder = db.get(WatchedFolder, folder_id)
    if folder is None:
        raise HTTPException(404, "Folder not found")
    manager.remove(folder.id)
    if remove_works:
        src_id = _local_source_id(db)
        if src_id is not None:
            for work in db.scalars(
                select(Work).where(
                    Work.source_id == src_id,
                    Work.source_work_ref.like(f"localfolder:{folder.id}:%"),
                )
            ).all():
                purge_work(db, work)   # clear memberships/hooks too, not a bare delete
    db.delete(folder)
    db.commit()
    return {"deleted": folder_id}
