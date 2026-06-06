"""Bookshelves — per-user organization of the library.

A user can create multiple shelves and place a work on 0+ of them. Each shelf carries automation
toggles (auto-update / auto-Kindle / notify-on-add) and can be marked as the destination for the
user's Goodreads auto-hooks. Shelves are strictly per-user; a work placed on a shelf is implicitly
in the user's library.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ..auth import current_user
from ..db import get_db
from ..library import add_to_library, in_library
from ..models import Bookshelf, BookshelfItem, User, Work
from ..schemas import BookshelfIn, BookshelfOut, BookshelfUpdate

router = APIRouter()


def _owned(db: Session, user: User, shelf_id: int) -> Bookshelf:
    shelf = db.get(Bookshelf, shelf_id)
    if shelf is None or shelf.user_id != user.id:
        raise HTTPException(404, "Bookshelf not found")
    return shelf


def _out(db: Session, shelf: Bookshelf) -> BookshelfOut:
    out = BookshelfOut.model_validate(shelf)
    out.count = db.scalar(
        select(func.count(BookshelfItem.id)).where(BookshelfItem.shelf_id == shelf.id)
    ) or 0
    return out


_SHELF_FLAGS = ("auto_update", "auto_kindle", "notify_on_add", "notify_email", "goodreads_target")


def _sync_shelf_folder(db: Session, shelf: Bookshelf) -> None:
    """Reconcile the WatchedFolder backing this shelf's watch_path: create/update it (and start
    watching) when a path is set, or remove it when cleared."""
    from ..ingestion.watcher import manager
    from ..models import WatchedFolder

    existing = db.scalar(select(WatchedFolder).where(WatchedFolder.shelf_id == shelf.id))
    path = (shelf.watch_path or "").strip()
    if not path:
        if existing is not None:
            try:
                manager.remove(existing.id)
            except Exception:  # noqa: BLE001
                pass
            db.delete(existing)
            db.commit()
        return
    if existing is not None:
        existing.path = path
        existing.user_id = shelf.user_id
        existing.enabled = True
        db.commit()
        try:
            manager.add(existing.id, existing.path, existing.recursive)
        except Exception:  # noqa: BLE001
            pass
        return
    # New mapping: refuse to hijack a path already watched by something else (path is unique).
    clash = db.scalar(select(WatchedFolder).where(WatchedFolder.path == path))
    if clash is not None:
        # Don't leave the shelf claiming a path that has no backing folder.
        shelf.watch_path = None
        db.commit()
        raise HTTPException(409, "That path is already watched by another folder/shelf.")
    wf = WatchedFolder(path=path, display_name=f"Shelf: {shelf.name}", recursive=True,
                       enabled=True, shelf_id=shelf.id, user_id=shelf.user_id)
    db.add(wf)
    db.commit()
    db.refresh(wf)
    try:
        manager.add(wf.id, wf.path, wf.recursive)
    except Exception:  # noqa: BLE001
        pass


def _require_admin_for_path(user: User, watch_path: str | None) -> None:
    """Setting a shelf's watch_path reads the host filesystem, so it's admin-only."""
    if watch_path and watch_path.strip() and user.role != "admin":
        raise HTTPException(403, "Only an admin can map a host path to a shelf.")


@router.get("/bookshelves", response_model=list[BookshelfOut])
def list_bookshelves(
    user: User = Depends(current_user), db: Session = Depends(get_db)
) -> list[BookshelfOut]:
    shelves = db.scalars(
        select(Bookshelf).where(Bookshelf.user_id == user.id)
        .order_by(Bookshelf.sort_order, Bookshelf.id)
    ).all()
    return [_out(db, s) for s in shelves]


@router.post("/bookshelves", response_model=BookshelfOut)
def create_bookshelf(
    payload: BookshelfIn, user: User = Depends(current_user), db: Session = Depends(get_db)
) -> BookshelfOut:
    name = payload.name.strip()
    if db.scalar(select(Bookshelf.id).where(
            Bookshelf.user_id == user.id, Bookshelf.name == name)):
        raise HTTPException(409, "You already have a bookshelf with that name.")
    nxt = (db.scalar(select(func.max(Bookshelf.sort_order)).where(
        Bookshelf.user_id == user.id)) or 0) + 1
    _require_admin_for_path(user, payload.watch_path)
    shelf = Bookshelf(
        user_id=user.id, name=name, sort_order=nxt,
        auto_update=payload.auto_update, auto_kindle=payload.auto_kindle,
        notify_on_add=payload.notify_on_add, notify_email=payload.notify_email,
        goodreads_target=payload.goodreads_target,
        goodreads_shelf=(payload.goodreads_shelf or "").strip() or None,
        watch_path=(payload.watch_path or "").strip() or None,
    )
    db.add(shelf)
    db.commit()
    db.refresh(shelf)
    if shelf.watch_path:
        _sync_shelf_folder(db, shelf)
    # Place any initial works (only those actually in the caller's library / admin).
    for wid in dict.fromkeys(payload.work_ids or []):
        if db.get(Work, wid) is None:
            continue
        if user.role == "admin" or in_library(db, user.id, wid):
            add_to_library(db, user.id, wid, shelf_id=shelf.id)
    return _out(db, shelf)


@router.patch("/bookshelves/{shelf_id}", response_model=BookshelfOut)
def update_bookshelf(
    shelf_id: int, payload: BookshelfUpdate,
    user: User = Depends(current_user), db: Session = Depends(get_db),
) -> BookshelfOut:
    shelf = _owned(db, user, shelf_id)
    data = payload.model_dump(exclude_unset=True)
    if "name" in data and data["name"]:
        name = data["name"].strip()
        clash = db.scalar(select(Bookshelf.id).where(
            Bookshelf.user_id == user.id, Bookshelf.name == name, Bookshelf.id != shelf_id))
        if clash:
            raise HTTPException(409, "You already have a bookshelf with that name.")
        shelf.name = name
    for f in ("sort_order", "auto_update", "auto_kindle", "notify_on_add", "notify_email",
              "goodreads_target"):
        if f in data and data[f] is not None:
            setattr(shelf, f, data[f])
    if "goodreads_shelf" in data:  # may be explicitly cleared (None / "")
        shelf.goodreads_shelf = (data["goodreads_shelf"] or "").strip() or None
    path_changed = False
    if "watch_path" in data:  # admin-only; may be explicitly cleared
        new_path = (data["watch_path"] or "").strip() or None
        if new_path != shelf.watch_path:
            _require_admin_for_path(user, new_path)
            shelf.watch_path = new_path
            path_changed = True
    db.commit()
    db.refresh(shelf)
    if path_changed:
        _sync_shelf_folder(db, shelf)
    return _out(db, shelf)


@router.delete("/bookshelves/{shelf_id}")
def delete_bookshelf(
    shelf_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)
) -> dict:
    """Delete a shelf. Works stay in the library (and on any other shelves) — only the shelf and
    its placements are removed."""
    shelf = _owned(db, user, shelf_id)
    if shelf.watch_path:
        shelf.watch_path = None
        db.commit()
        _sync_shelf_folder(db, shelf)  # stop watching + drop the backing folder
    db.execute(delete(BookshelfItem).where(BookshelfItem.shelf_id == shelf_id))
    db.execute(delete(Bookshelf).where(Bookshelf.id == shelf_id))
    db.commit()
    return {"deleted": shelf_id}


@router.post("/bookshelves/{shelf_id}/works/{work_id}", response_model=BookshelfOut)
def add_work(
    shelf_id: int, work_id: int,
    user: User = Depends(current_user), db: Session = Depends(get_db),
) -> BookshelfOut:
    """Place a work on the shelf. The work must be in the caller's library (admins/members);
    placing it also ensures membership."""
    shelf = _owned(db, user, shelf_id)
    if db.get(Work, work_id) is None:
        raise HTTPException(404, "Work not found")
    if user.role != "admin" and not in_library(db, user.id, work_id):
        raise HTTPException(404, "Work not found")  # can only shelve works in your library
    add_to_library(db, user.id, work_id, shelf_id=shelf_id)
    return _out(db, shelf)


@router.delete("/bookshelves/{shelf_id}/works/{work_id}", response_model=BookshelfOut)
def remove_work(
    shelf_id: int, work_id: int,
    user: User = Depends(current_user), db: Session = Depends(get_db),
) -> BookshelfOut:
    shelf = _owned(db, user, shelf_id)
    db.execute(delete(BookshelfItem).where(
        BookshelfItem.shelf_id == shelf_id, BookshelfItem.work_id == work_id))
    db.commit()
    return _out(db, shelf)
