"""Per-user library membership.

The library is PER-USER: a ``LibraryItem`` is a user's membership of a (global, shared) ``Work``.
Works + their crawl/chapters are shared across users — one crawl serves everyone — so hooking a
title that's already hooked just adds a membership and never re-crawls. Bookshelves organize a
user's library; placing a work on a shelf implies membership.
"""
from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .models import Bookshelf, BookshelfItem, LibraryItem, User


def assert_work_access(db: Session, user: User, work_id: int) -> None:
    """Library isolation gate: raise 404 unless ``work_id`` is in ``user``'s library (admins may
    access any). 404 (not 403) so a non-member can't probe which works exist. Use on every
    endpoint that returns or acts on a specific work's content (chapters, progress, delivery)."""
    if user.role == "admin":
        return
    if not in_library(db, user.id, work_id):
        raise HTTPException(404, "Work not found")


def add_to_library(db: Session, user_id: int, work_id: int, *, shelf_id: int | None = None) -> bool:
    """Ensure ``work_id`` is in ``user_id``'s library (idempotent). Returns True if newly added.
    With ``shelf_id``, also place it on that bookshelf (the shelf must belong to the user)."""
    added = False
    exists = db.scalar(
        select(LibraryItem.id).where(
            LibraryItem.user_id == user_id, LibraryItem.work_id == work_id
        )
    )
    if not exists:
        db.add(LibraryItem(user_id=user_id, work_id=work_id))
        added = True
    if shelf_id is not None:
        owns = db.scalar(
            select(Bookshelf.id).where(Bookshelf.id == shelf_id, Bookshelf.user_id == user_id)
        )
        on_shelf = db.scalar(
            select(BookshelfItem.id).where(
                BookshelfItem.shelf_id == shelf_id, BookshelfItem.work_id == work_id
            )
        )
        if owns and not on_shelf:
            db.add(BookshelfItem(shelf_id=shelf_id, work_id=work_id))
    db.commit()
    return added


def in_library(db: Session, user_id: int, work_id: int) -> bool:
    return db.scalar(
        select(LibraryItem.id).where(
            LibraryItem.user_id == user_id, LibraryItem.work_id == work_id
        )
    ) is not None


def library_work_ids(db: Session, user_id: int) -> set[int]:
    return set(db.scalars(select(LibraryItem.work_id).where(LibraryItem.user_id == user_id)).all())


def remove_from_library(db: Session, user_id: int, work_id: int) -> None:
    """Drop the user's membership + remove the work from any of THEIR bookshelves. The shared Work
    (and its chapters/crawl) is left intact — other users may still have it. Global deletion of the
    Work is a separate, explicit admin action."""
    db.execute(
        delete(LibraryItem).where(
            LibraryItem.user_id == user_id, LibraryItem.work_id == work_id
        )
    )
    shelf_ids = select(Bookshelf.id).where(Bookshelf.user_id == user_id)
    db.execute(
        delete(BookshelfItem).where(
            BookshelfItem.work_id == work_id, BookshelfItem.shelf_id.in_(shelf_ids)
        )
    )
    db.commit()
