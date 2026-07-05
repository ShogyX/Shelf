"""Wave A — preview-read access gate (#3) + dead-stock hook repair (#4).

Preview: an in-stock (hooked) title the catalog would SHOW a user becomes readable without first
adding it to their library, while category-cap / 18+ gating and the enumeration 404 still hold.
Repair: catalog hooks to a deleted or empty-import Work are cleared so they stop reporting in_stock.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import delete

from app.db import SessionLocal, init_db
from app.library import assert_work_access, readable_in_stock, unhook_dead_stock
from app.models import (
    CatalogGroup, CatalogWork, Chapter, ChapterContent, CrawlJob, LibraryItem, User, Work,
)


@pytest.fixture(autouse=True)
def _clean():
    init_db()
    db = SessionLocal()
    for model in (CatalogGroup, Chapter, ChapterContent, CrawlJob, LibraryItem, Work, User):
        db.execute(delete(model))
    db.commit()
    db.close()
    yield


def _user(db, username="reader", role="user", **kw):
    # Explicit allowed_categories in visibility tests keeps them independent of any global default.
    u = User(username=username, password_hash="x", role=role, **kw)
    db.add(u); db.commit(); db.refresh(u)
    return u


def _readable_work(db, title="Readable Book", **kw):
    w = Work(title=title, media_kind="text", status="complete", **kw)
    db.add(w); db.commit(); db.refresh(w)
    ch = Chapter(work_id=w.id, index=1, fetch_status="fetched")
    db.add(ch); db.commit(); db.refresh(ch)
    cc = ChapterContent(chapter_id=ch.id, format="html", body="<p>hi</p>", word_count=1, checksum="c1")
    db.add(cc); db.commit(); db.refresh(cc)
    ch.content_id = cc.id
    db.commit()
    return w


def _empty_work(db, title="Empty Book", **kw):
    w = Work(title=title, media_kind="text", status="complete", **kw)
    db.add(w); db.commit(); db.refresh(w)
    return w


def _group(db, work_id=None, title="G", media_label="Book", is_adult=False):
    g = CatalogGroup(norm_key=title.lower(), media_bucket="text", title=title,
                     media_label=media_label, is_adult=is_adult, hooked_work_id=work_id)
    db.add(g); db.commit(); db.refresh(g)
    return g


# --------------------------------------------------------------- #3 preview read gate
def test_in_stock_visible_title_is_previewable():
    db = SessionLocal()
    u = _user(db, allowed_categories=["Book"])
    w = _readable_work(db)
    _group(db, work_id=w.id, media_label="Book")
    assert readable_in_stock(db, u, w.id) is True
    assert_work_access(db, u, w.id)  # does not raise
    db.close()


def test_non_stock_title_stays_404():
    db = SessionLocal()
    u = _user(db, allowed_categories=["Book"])
    w = _readable_work(db)  # readable, but no catalog group hooks it → not "in stock"
    assert readable_in_stock(db, u, w.id) is False
    with pytest.raises(HTTPException) as ei:
        assert_work_access(db, u, w.id)
    assert ei.value.status_code == 404
    db.close()


def test_library_membership_grants_access_without_stock():
    db = SessionLocal()
    u = _user(db)
    w = _readable_work(db)
    db.add(LibraryItem(user_id=u.id, work_id=w.id)); db.commit()
    assert_work_access(db, u, w.id)  # owned → fine even with no catalog group
    db.close()


def test_category_cap_blocks_preview():
    db = SessionLocal()
    u = _user(db, username="capped", allowed_categories=["Novel"])  # not allowed Books
    w = _readable_work(db)
    _group(db, work_id=w.id, media_label="Book")
    assert readable_in_stock(db, u, w.id) is False
    with pytest.raises(HTTPException):
        assert_work_access(db, u, w.id)
    db.close()


def test_adult_optout_blocks_preview():
    db = SessionLocal()
    # category allowed, but this user opted OUT of all 18+ → an adult title stays hidden.
    u = _user(db, username="clean", allowed_categories=["Book"], adult_categories=[])
    w = _readable_work(db)
    _group(db, work_id=w.id, media_label="Book", is_adult=True)
    assert readable_in_stock(db, u, w.id) is False
    with pytest.raises(HTTPException):
        assert_work_access(db, u, w.id)
    db.close()


def test_adult_optin_allows_preview():
    db = SessionLocal()
    # category allowed AND opted into 18+ for Books → the adult title IS previewable (positive case,
    # guarding against a future regression that inverts the adult branch).
    u = _user(db, username="grown", allowed_categories=["Book"], adult_categories=["Book"])
    w = _readable_work(db)
    _group(db, work_id=w.id, media_label="Book", is_adult=True)
    assert readable_in_stock(db, u, w.id) is True
    assert_work_access(db, u, w.id)  # does not raise
    db.close()


def test_admin_bypasses_gate():
    db = SessionLocal()
    a = _user(db, username="boss", role="admin")
    w = _empty_work(db)  # not even readable
    assert_work_access(db, a, w.id)  # admin → always allowed
    # admin also previews an in-stock adult title regardless of their own 18+ preference
    adult = _readable_work(db, title="Grown Up")
    _group(db, work_id=adult.id, title="grownup", media_label="Book", is_adult=True)
    a.adult_categories = []  # even opted fully out
    db.commit()
    assert readable_in_stock(db, a, adult.id) is True
    db.close()


# --------------------------------------------------------------- #4 dead-stock repair
def test_repair_unhooks_empty_and_dangling_keeps_readable_and_crawling():
    db = SessionLocal()
    readable = _readable_work(db, title="Keeper")
    g_keep = _group(db, work_id=readable.id, title="keeper")

    empty = _empty_work(db, title="Failed Import")          # hooked, but no content, no crawl
    g_empty = _group(db, work_id=empty.id, title="failed")
    # A CatalogWork pointer to the same dead Work must also be cleared (not just the group).
    cw_empty = CatalogWork(provider="web_index", domain="x.test", work_url="https://x.test/1",
                           title="Failed Import", norm_key="failed", media_kind="text",
                           hooked_work_id=empty.id)
    db.add(cw_empty); db.commit(); db.refresh(cw_empty)

    crawling = _empty_work(db, title="Mid Crawl")           # no content YET, but actively crawling
    db.add(CrawlJob(work_id=crawling.id, kind="index", status="running")); db.commit()
    g_crawl = _group(db, work_id=crawling.id, title="crawling")

    g_dangling = _group(db, work_id=999999, title="ghost")  # points at a deleted Work

    counts = unhook_dead_stock(db)

    for g in (g_keep, g_empty, g_crawl, g_dangling):
        db.refresh(g)
    db.refresh(cw_empty)
    assert g_keep.hooked_work_id == readable.id     # readable → kept
    assert g_crawl.hooked_work_id == crawling.id    # mid-crawl → kept
    assert g_empty.hooked_work_id is None           # empty import → unhooked
    assert g_dangling.hooked_work_id is None        # dangling → unhooked
    assert cw_empty.hooked_work_id is None          # CatalogWork pointer also cleared
    assert counts["catalog_groups"] == 2
    assert counts["catalog_works"] == 1
    db.close()


def test_repair_noop_on_clean_db():
    db = SessionLocal()
    readable = _readable_work(db, title="Fine")
    _group(db, work_id=readable.id, title="fine")
    counts = unhook_dead_stock(db)  # nothing dead → all zero, no error
    assert counts == {"catalog_groups": 0, "catalog_works": 0, "indexed_pages": 0, "queued_hooks": 0}
    db.close()
