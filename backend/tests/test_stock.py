"""Library stocking: queue a selection, the worker searches usenet + grabs (operator-owned), and an
import flips the row to 'stocked' so the work is instantly available."""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import delete, func, select

import app.ingestion.adapters  # noqa: F401
from app.db import SessionLocal, init_db
from app.ingestion import stock as stock_mod
from app.ingestion.catalog_groups import regroup_catalog
from app.models import (
    CatalogGroup,
    CatalogWork,
    DownloadJob,
    Integration,
    Source,
    StockItem,
    StockJob,
    Work,
)


@pytest.fixture
def db():
    init_db()
    s = SessionLocal()
    for m in (StockItem, StockJob, DownloadJob, CatalogGroup, CatalogWork, Integration, Work):
        s.execute(delete(m))
    from app.models import AppSetting
    s.execute(delete(AppSetting))
    s.commit()
    yield s
    s.close()


def _pipeline(db):
    db.add(Integration(kind="prowlarr", name="P", base_url="http://p", api_key="k", enabled=True))
    db.add(Integration(kind="sabnzbd", name="S", base_url="http://s", api_key="k", enabled=True))
    db.commit()


def _cw(db, title, domain="comix.to", media="comic", pop=100.0, hooked=None):
    cw = CatalogWork(domain=domain, work_url=f"https://{domain}/t/{title.replace(' ', '-')}",
                     title=title, norm_key=title.lower(), media_kind=media, popularity=pop,
                     hooked_work_id=hooked)
    db.add(cw); db.commit()
    return cw


def test_stock_configured_gate(db):
    assert not stock_mod.stock_configured(db)          # nothing set
    _pipeline(db)
    assert not stock_mod.stock_configured(db)          # pipeline but no dir
    stock_mod.set_stock_dir(db, "/tmp/stock")
    assert stock_mod.stock_configured(db)              # both → ready
    assert stock_mod.get_stock_dir(db) == "/tmp/stock"


def test_queue_selection_skips_hooked_and_dedupes(db):
    _pipeline(db); stock_mod.set_stock_dir(db, "/tmp/stock")
    _cw(db, "Free Comic", pop=200)
    _cw(db, "Already Hooked", pop=150, hooked=999)      # already available → skipped
    regroup_catalog(db)

    res = stock_mod.queue_selection(db, limit=50)
    assert res["queued"] == 1 and res["selected"] == 1  # only the un-hooked group
    items = db.scalars(select(StockItem)).all()
    assert len(items) == 1 and items[0].title == "Free Comic" and items[0].status == "pending"
    assert items[0].media_category == "Manga & Comics"

    # Re-queueing the same selection adds nothing (deduped by norm_key).
    res2 = stock_mod.queue_selection(db, limit=50)
    assert res2["queued"] == 0 and res2["skipped"] == 1


def test_queue_selection_filters_by_media_category(db):
    _pipeline(db); stock_mod.set_stock_dir(db, "/tmp/stock")
    _cw(db, "A Comic", domain="comix.to", media="comic", pop=100)
    _cw(db, "A Novel", domain="ranobedb.org", media="text", pop=90)
    regroup_catalog(db)
    stock_mod.queue_selection(db, media="Manga & Comics", limit=50)
    items = db.scalars(select(StockItem)).all()
    assert {i.title for i in items} == {"A Comic"}      # the novel is excluded


def test_worker_grabs_then_import_marks_stocked(db, monkeypatch):
    _pipeline(db); stock_mod.set_stock_dir(db, "/tmp/stock")
    cw = _cw(db, "Stock Me", pop=100)
    regroup_catalog(db)
    stock_mod.queue_selection(db, limit=50)

    # Mock the usenet search + grab so no network is touched.
    from app.ingestion import downloads, release_matcher as rm

    async def _find(db_, book, **kw):
        return ["fake-ranked"]
    monkeypatch.setattr(rm, "find_releases", _find)
    monkeypatch.setattr(rm, "candidate_dicts",
                        lambda ranked, **kw: [{"download_url": "http://x/1.nzb", "title": "rel"}])

    grabbed = {}

    async def _grab(db_, catalog_work, *, candidates=None, user_id=None, kind="manual", **kw):
        assert user_id is None and kind == "stock"     # operator-owned stock grab
        job = DownloadJob(catalog_work_id=catalog_work.id, title=catalog_work.title,
                          status="downloading", grab_kind="stock")
        db_.add(job); db_.commit(); db_.refresh(job)
        grabbed["job_id"] = job.id
        return job
    monkeypatch.setattr(downloads, "grab_release", _grab)

    asyncio.run(stock_mod.stock_tick())
    si = db.scalar(select(StockItem))
    assert si.status == "downloading" and si.download_job_id == grabbed["job_id"]

    # Simulate the import completing: a Work lands + the stock hook fires.
    src = db.scalar(select(Source)) or Source(key="local_folder", display_name="lf",
                                              adapter_key="local_folder", tos_permitted=True)
    if src.id is None:
        db.add(src); db.commit()
    work = Work(source_id=src.id, source_work_ref="stock:1", title="Stock Me", status="complete",
                local_path="/tmp/stock/Stock Me/book.epub", local_size=1234)
    db.add(work); db.commit(); db.refresh(work)
    job = db.get(DownloadJob, grabbed["job_id"])
    job.work_id = work.id; job.status = "imported"; db.commit()

    stock_mod.on_stock_imported(db, job)
    db.refresh(si)
    assert si.status == "stocked" and si.work_id == work.id
    assert si.file_path == "/tmp/stock/Stock Me/book.epub" and si.size == 1234
    # The group is hooked immediately so user acquires hit it.
    grp = db.get(CatalogGroup, cw.id)
    assert grp is not None and grp.hooked_work_id == work.id


def test_worker_marks_unavailable_when_no_release(db, monkeypatch):
    _pipeline(db); stock_mod.set_stock_dir(db, "/tmp/stock")
    _cw(db, "Obscure Title", pop=100)
    regroup_catalog(db)
    stock_mod.queue_selection(db, limit=50)

    from app.ingestion import release_matcher as rm

    async def _find(db_, book, **kw):
        return []
    monkeypatch.setattr(rm, "find_releases", _find)
    monkeypatch.setattr(rm, "candidate_dicts", lambda ranked, **kw: [])

    asyncio.run(stock_mod.stock_tick())
    si = db.scalar(select(StockItem))
    assert si.status == "unavailable" and "no matching usenet release" in (si.error or "")


def test_stock_tick_noop_when_unconfigured(db):
    out = asyncio.run(stock_mod.stock_tick())
    assert out.get("skipped") == "not configured"


# ---- Named stock jobs (batches) -------------------------------------------------------------

def test_queue_creates_named_job_and_attaches_items(db):
    _pipeline(db); stock_mod.set_stock_dir(db, "/tmp/stock")
    _cw(db, "Alpha", pop=200); _cw(db, "Beta", pop=150)
    regroup_catalog(db)
    res = stock_mod.queue_selection(db, name="My Batch", limit=50)
    assert res["job_id"] and res["name"] == "My Batch" and res["queued"] == 2
    job = db.get(StockJob, res["job_id"])
    assert job is not None and job.requested == 2
    items = db.scalars(select(StockItem).where(StockItem.stock_job_id == job.id)).all()
    assert len(items) == 2


def test_queue_without_name_derives_one(db):
    _pipeline(db); stock_mod.set_stock_dir(db, "/tmp/stock")
    _cw(db, "Solo", pop=100)
    regroup_catalog(db)
    res = stock_mod.queue_selection(db, media="Manga & Comics", limit=50)
    assert res["job_id"] and "Manga & Comics" in res["name"]


def test_queue_with_no_new_items_makes_no_empty_job(db):
    _pipeline(db); stock_mod.set_stock_dir(db, "/tmp/stock")
    _cw(db, "Dup", pop=100)
    regroup_catalog(db)
    stock_mod.queue_selection(db, name="First", limit=50)        # claims "Dup"
    res = stock_mod.queue_selection(db, name="Second", limit=50)  # nothing new
    assert res["job_id"] is None and res["queued"] == 0
    assert db.scalar(select(func.count(StockJob.id))) == 1        # no empty 'Second' job left behind


def test_list_jobs_rolls_up_stats(db):
    _pipeline(db); stock_mod.set_stock_dir(db, "/tmp/stock")
    _cw(db, "One", pop=100); _cw(db, "Two", pop=90); _cw(db, "Three", pop=80)
    regroup_catalog(db)
    res = stock_mod.queue_selection(db, name="Batch", limit=50)
    items = db.scalars(select(StockItem).where(StockItem.stock_job_id == res["job_id"])
                       .order_by(StockItem.id)).all()
    items[0].status = "stocked"; items[0].size = 1_000_000
    items[1].status = "failed"; items[1].error = "boom"
    db.commit()
    jobs = stock_mod.list_jobs(db)
    j = next(x for x in jobs if x["id"] == res["job_id"])
    assert j["total"] == 3 and j["stocked"] == 1 and j["issues"] == 1 and j["pending"] == 1
    assert 0.0 < j["progress"] < 1.0 and j["overall"] == "working" and j["stocked_size"] == 1_000_000


def test_job_detail_and_retry_issues(db):
    _pipeline(db); stock_mod.set_stock_dir(db, "/tmp/stock")
    _cw(db, "X", pop=100); _cw(db, "Y", pop=90)
    regroup_catalog(db)
    res = stock_mod.queue_selection(db, name="B", limit=50)
    its = db.scalars(select(StockItem).where(StockItem.stock_job_id == res["job_id"])).all()
    its[0].status = "failed"; its[0].error = "no release"
    db.commit()
    detail = stock_mod.job_detail(db, res["job_id"])
    assert detail["name"] == "B" and len(detail["items"]) == 2 and len(detail["problem_items"]) == 1
    n = stock_mod.retry_job_issues(db, res["job_id"])
    assert n == 1
    db.expire_all()
    assert db.scalar(select(func.count(StockItem.id)).where(
        StockItem.stock_job_id == res["job_id"], StockItem.status == "pending")) == 2


def test_remove_job_deletes_items(db):
    _pipeline(db); stock_mod.set_stock_dir(db, "/tmp/stock")
    _cw(db, "Z", pop=100)
    regroup_catalog(db)
    res = stock_mod.queue_selection(db, name="ToDelete", limit=50)
    assert stock_mod.remove_job(db, res["job_id"], delete_files=False) is True
    assert db.get(StockJob, res["job_id"]) is None
    assert db.scalar(select(func.count(StockItem.id)).where(
        StockItem.stock_job_id == res["job_id"])) == 0


def test_ungrouped_bucket_for_legacy_items(db):
    _pipeline(db); stock_mod.set_stock_dir(db, "/tmp/stock")
    # a legacy item with no job (pre-batches)
    db.add(StockItem(norm_key="legacy", title="Legacy", status="pending", stock_job_id=None))
    db.commit()
    jobs = stock_mod.list_jobs(db)
    bucket = next(x for x in jobs if x["id"] is None)
    assert bucket["total"] == 1
    detail = stock_mod.job_detail(db, 0)  # 0 → ungrouped
    assert detail is not None and len(detail["items"]) == 1
