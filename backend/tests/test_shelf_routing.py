"""Operator shelf routing: hook/acquire actions land the title on a chosen bookshelf, and a
shelf that isn't the caller's is rejected (never silently dropped)."""
from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, select

from app.db import SessionLocal, init_db
from app.library import validate_shelf
from app.models import (
    Bookshelf,
    BookshelfItem,
    CatalogWork,
    LibraryItem,
    Source,
    User,
    Work,
)
from app.routers.index import hook_catalog


@pytest.fixture
def db():
    init_db()
    s = SessionLocal()
    for m in (LibraryItem, BookshelfItem, Bookshelf, CatalogWork, Work, User):
        s.execute(delete(m))
    s.commit()
    yield s
    s.close()


def _user(db, name="bob") -> User:
    u = User(username=name, password_hash="x", role="user", is_active=True)
    db.add(u); db.commit(); db.refresh(u)
    return u


def _shelf(db, user_id: int, name="Reading") -> Bookshelf:
    sh = Bookshelf(user_id=user_id, name=name)
    db.add(sh); db.commit(); db.refresh(sh)
    return sh


def _hooked_catalog(db) -> tuple[CatalogWork, Work]:
    src = db.scalar(select(Source).where(Source.key == "web_index"))
    if src is None:
        src = Source(key="web_index", display_name="wi", adapter_key="web_index", tos_permitted=True)
        db.add(src); db.commit()
    w = Work(source_id=src.id, source_work_ref="ref-x", title="Hooked", hooked=True, status="complete")
    db.add(w); db.commit(); db.refresh(w)
    cw = CatalogWork(provider="web_index", title="Hooked", norm_key="hooked", hooked_work_id=w.id,
                     domain="example.com", work_url="https://example.com/hooked")
    db.add(cw); db.commit(); db.refresh(cw)
    return cw, w


def test_validate_shelf(db):
    u = _user(db)
    sh = _shelf(db, u.id)
    other = _user(db, "eve")
    foreign = _shelf(db, other.id, "Eve's")
    assert validate_shelf(db, u.id, None) is None
    assert validate_shelf(db, u.id, sh.id) == sh.id
    with pytest.raises(HTTPException) as ei:        # someone else's shelf
        validate_shelf(db, u.id, foreign.id)
    assert ei.value.status_code == 400
    with pytest.raises(HTTPException):              # nonexistent shelf
        validate_shelf(db, u.id, 999999)


def test_hook_places_on_chosen_shelf(db):
    u = _user(db)
    sh = _shelf(db, u.id)
    cw, w = _hooked_catalog(db)
    # Membership-only hook (already-hooked work) onto the chosen shelf.
    asyncio.run(hook_catalog(cw.id, start_chapter=1, shelf_id=sh.id, user=u, db=db))
    placed = db.scalar(select(BookshelfItem).where(
        BookshelfItem.shelf_id == sh.id, BookshelfItem.work_id == w.id))
    assert placed is not None
    assert db.scalar(select(LibraryItem).where(
        LibraryItem.user_id == u.id, LibraryItem.work_id == w.id)) is not None


def test_hook_rejects_foreign_shelf(db):
    u = _user(db)
    other = _user(db, "eve")
    foreign = _shelf(db, other.id, "Eve's")
    cw, _ = _hooked_catalog(db)
    with pytest.raises(HTTPException) as ei:
        asyncio.run(hook_catalog(cw.id, start_chapter=1, shelf_id=foreign.id, user=u, db=db))
    assert ei.value.status_code == 400
