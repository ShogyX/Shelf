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

    # Re-queueing the same selection adds nothing — an already-stocked title is no longer even
    # SELECTED (the selection naturally avoids titles already in the stock list).
    res2 = stock_mod.queue_selection(db, limit=50)
    assert res2["queued"] == 0 and res2["selected"] == 0


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

    # C1: re-running the import hook for the SAME (item, work) is a no-op — three uncoordinated
    # paths can fire it, and a second _migrate_work_links for the same ids would double-move rows.
    stock_mod.on_stock_imported(db, job)
    stock_mod.on_stock_imported(db, job)
    db.refresh(si)
    assert si.status == "stocked" and si.work_id == work.id


def test_mark_stocked_is_idempotent_and_migrates_once(db, monkeypatch):
    """_mark_stocked: a no-op when already stocked for the same work; on a re-fetch (different
    work id) it migrates links exactly once."""
    from app.models import LibraryItem, User
    u = User(username="m1", password_hash="h", role="user"); db.add(u); db.commit(); db.refresh(u)
    src = db.scalar(select(Source)) or None
    if src is None:
        src = Source(key="local_folder", display_name="lf", adapter_key="local_folder",
                     tos_permitted=True)
        db.add(src); db.commit(); db.refresh(src)
    w1 = Work(source_id=src.id, source_work_ref="m:1", title="M", local_path="/s/m1.epub")
    w2 = Work(source_id=src.id, source_work_ref="m:2", title="M", local_path="/s/m2.epub")
    db.add_all([w1, w2]); db.commit(); db.refresh(w1); db.refresh(w2)
    si = StockItem(norm_key="m", title="M", status="pending"); db.add(si); db.commit()

    stock_mod._mark_stocked(db, si, w1.id); db.commit()
    assert si.status == "stocked" and si.work_id == w1.id
    # idempotent: same work again → early-return, no membership churn
    db.add(LibraryItem(user_id=u.id, work_id=w1.id)); db.commit()
    calls = {"n": 0}
    real = stock_mod._migrate_work_links
    monkeypatch.setattr(stock_mod, "_migrate_work_links",
                        lambda *a, **k: (calls.__setitem__("n", calls["n"] + 1), real(*a, **k))[1])
    stock_mod._mark_stocked(db, si, w1.id); db.commit()
    assert calls["n"] == 0                              # no migration for the same work
    # re-fetch to a NEW work → migrate exactly once, membership follows
    stock_mod._mark_stocked(db, si, w2.id); db.commit()
    assert calls["n"] == 1 and si.work_id == w2.id
    assert db.scalar(select(LibraryItem).where(LibraryItem.user_id == u.id)).work_id == w2.id


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
    # No usenet release → marked unavailable and HANDED OFF to the dedicated open-library worker
    # (stock_tick must NOT run the slow libgen download inline — I2).
    assert si.status == "unavailable" and "open-library fallback worker" in (si.error or "")


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


def test_job_detail_caps_items(db):
    # The ungrouped bucket can hold thousands of items; job_detail must cap the sample it ships while
    # keeping the totals accurate (computed from grouped counts, not the capped list).
    _pipeline(db); stock_mod.set_stock_dir(db, "/tmp/stock")
    for i in range(10):
        _cw(db, f"T{i:02d}", pop=100 - i)
    regroup_catalog(db)
    res = stock_mod.queue_selection(db, name="Big", limit=50)
    detail = stock_mod.job_detail(db, res["job_id"], item_cap=4, problem_cap=2)
    assert detail["total"] == 10                      # accurate, from counts
    assert detail["items_shown"] == 4 and len(detail["items"]) == 4   # capped sample


def test_stock_libgen_recovery_is_decoupled_from_stock_tick(db, monkeypatch):
    # The slow open-library recovery must run on its OWN tick, never inside stock_tick (where it would
    # block reconcile/progress updates).
    _pipeline(db); stock_mod.set_stock_dir(db, "/tmp/stock")
    db.add(Integration(kind="libgen", name="OL", base_url="", api_key="", enabled=True, config=None))
    db.commit()
    calls = {"retry": 0}

    async def _retry(_db, **k):
        calls["retry"] += 1
        return {"tried": 0, "stocked": 0}
    monkeypatch.setattr(stock_mod, "retry_failed_via_libgen", _retry)

    asyncio.run(stock_mod.stock_tick())
    assert calls["retry"] == 0                          # stock_tick must NOT run the recovery
    asyncio.run(stock_mod.stock_libgen_tick())
    assert calls["retry"] == 1                          # the dedicated tick does


def test_retry_failed_via_libgen_respects_budget(db, monkeypatch):
    # A zero time-budget must stop the loop before attempting any item (so a slow run can't sprawl).
    _pipeline(db); stock_mod.set_stock_dir(db, "/tmp/stock")
    db.add(Integration(kind="libgen", name="OL", base_url="", api_key="", enabled=True, config=None))
    _cw(db, "Budgeted", pop=100)
    regroup_catalog(db)
    stock_mod.queue_selection(db, name="B", limit=50)
    for si in db.scalars(select(StockItem)).all():
        si.status, si.error = "failed", "no release"
    db.commit()

    async def _boom(*a, **k):
        raise AssertionError("must not attempt an item past the budget")
    monkeypatch.setattr(stock_mod, "_try_libgen", _boom)
    out = asyncio.run(stock_mod.retry_failed_via_libgen(db, limit=10, budget_s=0.0))
    assert out["tried"] == 0


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


def test_libgen_fallback_stocks_when_usenet_has_nothing(db, monkeypatch):
    """Usenet finds no release → stock_tick marks the item unavailable (NOT running libgen inline,
    I2), then the dedicated open-library worker (stock_libgen_tick) recovers + stocks it."""
    _pipeline(db); stock_mod.set_stock_dir(db, "/tmp/stock")
    db.add(Integration(kind="libgen", name="OL", base_url="", api_key="", enabled=True, config=None))
    db.commit()
    cw = _cw(db, "Fallback Title", pop=100)
    regroup_catalog(db)
    stock_mod.queue_selection(db, name="B", limit=50)

    from app.ingestion import release_matcher as rm
    monkeypatch.setattr(rm, "find_releases", lambda *a, **k: _aret([]))
    monkeypatch.setattr(rm, "candidate_dicts", lambda ranked, **kw: [])

    # libgen.fetch_for_stock returns an "imported" job + a Work, simulating a successful recovery.
    from app.ingestion import libgen
    work = Work(title="Fallback Title", status="complete", local_path="/tmp/stock/x/book.epub", local_size=999)
    db.add(work); db.commit(); db.refresh(work)

    async def _fetch(db_, cw_, sdir):
        job = DownloadJob(catalog_work_id=cw_.id, title=cw_.title, status="imported",
                          grab_kind="libgen", work_id=work.id)
        db_.add(job); db_.commit(); db_.refresh(job)
        return job
    monkeypatch.setattr(libgen, "fetch_for_stock", _fetch)

    # Stage 1: the usenet tick defers — does NOT stock inline.
    asyncio.run(stock_mod.stock_tick())
    si = db.scalar(select(StockItem))
    assert si.status == "unavailable"
    # Stage 2: the dedicated libgen worker recovers + stocks it.
    asyncio.run(stock_mod.stock_libgen_tick())
    db.refresh(si)
    assert si.status == "stocked" and si.work_id == work.id


def test_retry_failed_via_libgen_recovers_and_tags(db, monkeypatch):
    _pipeline(db); stock_mod.set_stock_dir(db, "/tmp/stock")
    db.add(Integration(kind="libgen", name="OL", base_url="", api_key="", enabled=True, config=None))
    db.commit()
    a = _cw(db, "Recoverable", pop=100); b = _cw(db, "Truly Gone", pop=90)
    regroup_catalog(db)
    stock_mod.queue_selection(db, name="B", limit=50)
    # mark both as failed (usenet couldn't get them)
    for si in db.scalars(select(StockItem)).all():
        si.status, si.error = "failed", "download failed"
    db.commit()

    from app.ingestion import libgen
    rec = db.scalar(select(Work)) or Work(title="Recoverable", status="complete",
                                          local_path="/tmp/stock/r/b.epub", local_size=10)
    if rec.id is None:
        db.add(rec); db.commit(); db.refresh(rec)

    async def _fetch(db_, cw_, sdir):
        ok = cw_.title == "Recoverable"
        job = DownloadJob(catalog_work_id=cw_.id, title=cw_.title,
                          status="imported" if ok else "failed",
                          grab_kind="libgen", work_id=rec.id if ok else None,
                          error=None if ok else "no verifiable file")
        db_.add(job); db_.commit(); db_.refresh(job)
        return job
    monkeypatch.setattr(libgen, "fetch_for_stock", _fetch)

    out = asyncio.run(stock_mod.retry_failed_via_libgen(db, limit=10))
    assert out["tried"] == 2 and out["stocked"] == 1
    items = {i.title: i for i in db.scalars(select(StockItem)).all()}
    assert items["Recoverable"].status == "stocked"
    gone = items["Truly Gone"]
    assert gone.status in ("failed", "unavailable") and gone.error.startswith("open-library:")
    # a second run skips the already-tagged failure (no infinite retry)
    out2 = asyncio.run(stock_mod.retry_failed_via_libgen(db, limit=10))
    assert out2["tried"] == 0


def _aret(v):
    async def _c(*a, **k): return v
    return _c()


# ---- New: entire_catalog / exclude_web_index selection -------------------------------------------

def test_entire_catalog_selects_across_catalog_with_cap(db):
    _pipeline(db); stock_mod.set_stock_dir(db, "/tmp/stock")
    # A mix of media; entire_catalog ignores the filter and stocks the whole (eligible) catalog.
    _cw(db, "C One", domain="comix.to", media="comic", pop=100)
    _cw(db, "N One", domain="ranobedb.org", media="text", pop=90)
    _cw(db, "C Two", domain="comix.to", media="comic", pop=80)
    regroup_catalog(db)
    res = stock_mod.queue_selection(db, name="Everything", entire_catalog=True, limit=2)
    # The safety cap (limit=2) bounds it even though 3 groups are eligible.
    assert res["selected"] == 2 and res["queued"] == 2
    titles = {i.title for i in db.scalars(select(StockItem)).all()}
    assert len(titles) == 2 and titles <= {"C One", "N One", "C Two"}
    # Cross-media: the picked set isn't restricted to one category (popularity-ordered, top 2 here).
    assert "C One" in titles and "N One" in titles


def test_entire_catalog_overrides_media_filter(db):
    _pipeline(db); stock_mod.set_stock_dir(db, "/tmp/stock")
    _cw(db, "A Comic", domain="comix.to", media="comic", pop=100)
    _cw(db, "A Novel", domain="ranobedb.org", media="text", pop=90)
    regroup_catalog(db)
    # Even with a media filter passed, entire_catalog clears it → both selected.
    res = stock_mod.queue_selection(db, media="Manga & Comics", entire_catalog=True, limit=50)
    assert res["selected"] == 2


def test_exclude_web_index_drops_web_only_groups(db):
    _pipeline(db); stock_mod.set_stock_dir(db, "/tmp/stock")
    # Web-only group: its sole member is a crawled (web_index) source → dropped when excluded.
    _cw(db, "Web Only", domain="comix.to", media="comic", pop=100)   # provider defaults web_index
    # A group with a non-web member (Readarr) → kept even though it may also have web members.
    keep = CatalogWork(domain="readarr.local", work_url="https://readarr.local/b/keep",
                       title="Has Readarr", norm_key="has readarr", media_kind="text",
                       popularity=90.0, provider="readarr")
    db.add(keep); db.commit()
    regroup_catalog(db)

    groups = stock_mod._select_groups(db, media=None, dimension=None, value=None, sort="popularity",
                                      limit=50, group_ids=None, exclude_web_index=True)
    titles = {g.title for g in groups}
    assert "Has Readarr" in titles and "Web Only" not in titles
    # Without the flag, the web-only group is included.
    all_groups = stock_mod._select_groups(db, media=None, dimension=None, value=None,
                                          sort="popularity", limit=50, group_ids=None)
    assert {"Web Only", "Has Readarr"} <= {g.title for g in all_groups}


def test_exclude_web_index_keeps_group_with_mixed_members(db):
    _pipeline(db); stock_mod.set_stock_dir(db, "/tmp/stock")
    # Two members under the SAME norm_key (one crawled, one Readarr) → one group, kept.
    web = CatalogWork(domain="comix.to", work_url="https://comix.to/t/mixed", title="Mixed",
                      norm_key="mixed", media_kind="text", popularity=100.0, provider="web_index")
    rea = CatalogWork(domain="readarr.local", work_url="https://readarr.local/b/mixed", title="Mixed",
                      norm_key="mixed", media_kind="text", popularity=80.0, provider="readarr")
    db.add_all([web, rea]); db.commit()
    regroup_catalog(db)
    groups = stock_mod._select_groups(db, media=None, dimension=None, value=None, sort="popularity",
                                      limit=50, group_ids=None, exclude_web_index=True)
    assert "Mixed" in {g.title for g in groups}


# ---- New: daily search/download caps -------------------------------------------------------------

def _mock_grab(monkeypatch, *, cands=True):
    """Stub the usenet search + grab so no network is touched. find_releases returns one ranked hit
    (or none when cands=False); grab_release records a downloading stock job."""
    from app.ingestion import downloads, release_matcher as rm

    async def _find(db_, book, **kw):
        return ["ranked"] if cands else []
    monkeypatch.setattr(rm, "find_releases", _find)
    monkeypatch.setattr(rm, "candidate_dicts",
                        lambda ranked, **kw: ([{"download_url": "http://x/1.nzb", "title": "rel"}]
                                              if ranked else []))

    grabs = {"n": 0}

    async def _grab(db_, catalog_work, *, candidates=None, user_id=None, kind="manual", **kw):
        grabs["n"] += 1
        job = DownloadJob(catalog_work_id=catalog_work.id, title=catalog_work.title,
                          status="downloading", grab_kind="stock")
        db_.add(job); db_.commit(); db_.refresh(job)
        return job
    monkeypatch.setattr(downloads, "grab_release", _grab)
    return grabs


def _set_cap(db, **caps):
    from app import config_store
    config_store.update(db, caps)


def test_search_cap_gates_the_batch(db, monkeypatch):
    _pipeline(db); stock_mod.set_stock_dir(db, "/tmp/stock")
    for i in range(5):
        _cw(db, f"S{i}", pop=100 - i)
    regroup_catalog(db)
    stock_mod.queue_selection(db, limit=50)
    grabs = _mock_grab(monkeypatch)
    _set_cap(db, stock_searches_per_day=2)
    try:
        asyncio.run(stock_mod.stock_tick())
        # Only 2 searches allowed today → only 2 items advanced past pending (2 grabs).
        assert grabs["n"] == 2
        caps = stock_mod.daily_caps(db)
        assert caps["searches_per_day"] == 2 and caps["searches_used_today"] == 2
        # A second tick is fully capped — no more searches/grabs.
        asyncio.run(stock_mod.stock_tick())
        assert grabs["n"] == 2
        assert db.scalar(select(func.count(StockItem.id)).where(
            StockItem.status == "pending")) == 3      # the rest wait for tomorrow
    finally:
        _set_cap(db, stock_searches_per_day=0)


def test_at_cap_does_no_searches(db, monkeypatch):
    _pipeline(db); stock_mod.set_stock_dir(db, "/tmp/stock")
    for i in range(3):
        _cw(db, f"A{i}", pop=100 - i)
    regroup_catalog(db)
    stock_mod.queue_selection(db, limit=50)
    grabs = _mock_grab(monkeypatch)
    _set_cap(db, stock_searches_per_day=2)
    try:
        # Pre-load today's usage AT the cap → the tick must do nothing.
        stock_mod._bump_usage(db, searches=2); db.commit()
        out = asyncio.run(stock_mod.stock_tick())
        assert grabs["n"] == 0 and out.get("capped") is True
    finally:
        _set_cap(db, stock_searches_per_day=0)


def test_download_cap_leaves_item_pending(db, monkeypatch):
    _pipeline(db); stock_mod.set_stock_dir(db, "/tmp/stock")
    for i in range(3):
        _cw(db, f"D{i}", pop=100 - i)
    regroup_catalog(db)
    stock_mod.queue_selection(db, limit=50)
    grabs = _mock_grab(monkeypatch)
    _set_cap(db, stock_downloads_per_day=1)      # searches unlimited, downloads capped at 1
    try:
        asyncio.run(stock_mod.stock_tick())
        # All 3 (up to STOCK_PER_TICK) searched, but only 1 grabbed; the rest stay pending.
        assert grabs["n"] == 1
        caps = stock_mod.daily_caps(db)
        assert caps["downloads_used_today"] == 1
        # The 2 that searched-but-didn't-grab are back to pending (retried tomorrow).
        assert db.scalar(select(func.count(StockItem.id)).where(
            StockItem.status == "pending")) >= 2
        assert db.scalar(select(func.count(StockItem.id)).where(
            StockItem.status == "downloading")) == 1
    finally:
        _set_cap(db, stock_downloads_per_day=0)


def test_under_cap_proceeds_unlimited_by_default(db, monkeypatch):
    _pipeline(db); stock_mod.set_stock_dir(db, "/tmp/stock")
    for i in range(2):
        _cw(db, f"U{i}", pop=100 - i)
    regroup_catalog(db)
    stock_mod.queue_selection(db, limit=50)
    grabs = _mock_grab(monkeypatch)
    # Default caps are 0 (unlimited) → all pending (up to STOCK_PER_TICK) processed + grabbed.
    asyncio.run(stock_mod.stock_tick())
    assert grabs["n"] == 2
    caps = stock_mod.daily_caps(db)
    assert caps["searches_per_day"] == 0 and caps["downloads_per_day"] == 0
    assert caps["searches_used_today"] == 2 and caps["downloads_used_today"] == 2


def test_usage_counter_resets_on_new_utc_day(db, monkeypatch):
    # The durable per-day counter reads as zero once the stored UTC date no longer matches today.
    stock_mod._bump_usage(db, searches=5, downloads=3); db.commit()
    assert stock_mod._usage(db) == {"searches": 5, "downloads": 3}
    monkeypatch.setattr(stock_mod, "_today_utc", lambda: "1999-01-01")
    assert stock_mod._usage(db) == {"searches": 0, "downloads": 0}


# ---- New: summary surfaces caps + feeding lists --------------------------------------------------

def test_summary_reports_caps_and_usage(db):
    _pipeline(db); stock_mod.set_stock_dir(db, "/tmp/stock")
    _set_cap(db, stock_searches_per_day=10, stock_downloads_per_day=5)
    try:
        stock_mod._bump_usage(db, searches=3, downloads=1); db.commit()
        s = stock_mod.summary(db)
        caps = s["daily_caps"]
        assert caps["searches_per_day"] == 10 and caps["downloads_per_day"] == 5
        assert caps["searches_used_today"] == 3 and caps["downloads_used_today"] == 1
    finally:
        _set_cap(db, stock_searches_per_day=0, stock_downloads_per_day=0)


def test_summary_surfaces_feeding_lists(db):
    from app.models import ListSubscription, User
    u = User(username="lu", password_hash="h", role="user"); db.add(u); db.commit(); db.refresh(u)
    # A to_stock list (surfaced) + a library-bound list (not surfaced) + an inactive to_stock (hidden).
    db.add(ListSubscription(user_id=u.id, provider="goodreads", list_ref="gr1",
                            list_name="to-read", display_name="GR shelf", variant="ebook",
                            to_stock=True, active=True, auto_added=4))
    db.add(ListSubscription(user_id=u.id, provider="anilist", list_ref="al1",
                            display_name="AL list", to_stock=False, active=True))
    db.add(ListSubscription(user_id=u.id, provider="mal", list_ref="mal1",
                            display_name="MAL", to_stock=True, active=False))
    db.commit()
    feeding = stock_mod.summary(db)["feeding_lists"]
    assert len(feeding) == 1
    f = feeding[0]
    assert f["provider"] == "goodreads" and f["to_stock"] is True and f["auto_added"] == 4
    assert f["list_name"] == "to-read"


def test_sweep_integrity_refetches_corrupt(db, monkeypatch, tmp_path):
    _pipeline(db); stock_mod.set_stock_dir(db, str(tmp_path))
    cw = _cw(db, "Corrupt Book", pop=100)
    regroup_catalog(db)
    # a stocked item whose file is corrupt
    src = db.scalar(select(Source)) or Source(key="local_folder", display_name="lf",
                                              adapter_key="local_folder", tos_permitted=True)
    if src.id is None:
        db.add(src); db.commit()
    bookdir = tmp_path / "Corrupt Book"; bookdir.mkdir()
    f = bookdir / "book.epub"; f.write_bytes(b"not a real zip" + b"x" * 300)   # corrupt
    work = Work(source_id=src.id, source_work_ref="stock:c", title="Corrupt Book", status="complete",
                local_path=str(f), local_size=320)
    db.add(work); db.commit(); db.refresh(work)
    job = DownloadJob(catalog_work_id=cw.id, title="Corrupt Book", status="imported",
                      grab_kind="stock", work_id=work.id, release_key="guid:bad")
    db.add(job); db.commit(); db.refresh(job)
    grp = db.get(CatalogGroup, cw.id); grp.hooked_work_id = work.id
    si = db.scalar(select(StockItem)) or StockItem(norm_key=cw.norm_key, catalog_work_id=cw.id,
                                                   title="Corrupt Book", status="pending")
    if si.id is None:
        db.add(si); db.commit(); db.refresh(si)
    si.status = "stocked"; si.work_id = work.id; si.file_path = str(f); si.download_job_id = job.id
    db.commit()
    # a user has this stocked book in their library
    from app.models import LibraryItem
    db.add(LibraryItem(user_id=42, work_id=work.id)); db.commit()

    out = stock_mod.sweep_integrity(db)
    assert out["checked"] == 1 and out["corrupt"] == 1
    db.refresh(si)
    assert si.status == "pending" and si.file_path is None       # re-queued for a fresh download
    assert si.work_id == work.id                                  # kept as the rebind target
    assert not f.exists()                                         # bad file removed
    assert db.get(Work, work.id) is not None                     # Work PRESERVED (no user-data loss)
    assert db.scalar(select(LibraryItem.id).where(LibraryItem.work_id == work.id)) is not None  # user keeps it
    from app.ingestion import broken
    assert broken.is_broken(db, {"key": "guid:bad"})             # release won't be re-grabbed
    db.refresh(grp); assert grp.hooked_work_id is None           # un-hooked → re-fetch on new acquisition

    # Re-fetch produces a NEW Work → the user's library entry + shelf migrate onto it; old Work dropped.
    new = Work(source_id=src.id, source_work_ref="stock:c2", title="Corrupt Book", status="complete",
               local_path=str(tmp_path / "Corrupt Book" / "fresh.epub"), local_size=9999)
    db.add(new); db.commit(); db.refresh(new)
    stock_mod._mark_stocked(db, si, new.id); db.commit()
    assert si.status == "stocked" and si.work_id == new.id
    assert db.get(Work, work.id) is None                                              # old Work gone
    assert db.scalar(select(LibraryItem.id).where(LibraryItem.work_id == new.id)) is not None  # migrated


def test_sweep_integrity_keeps_good_files(db, tmp_path):
    import io, zipfile
    _pipeline(db); stock_mod.set_stock_dir(db, str(tmp_path))
    cw = _cw(db, "Good Book", pop=100); regroup_catalog(db)
    src = db.scalar(select(Source)) or Source(key="local_folder", display_name="lf",
                                              adapter_key="local_folder", tos_permitted=True)
    if src.id is None:
        db.add(src); db.commit()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml", '<container><rootfiles><rootfile full-path="c.opf"/></rootfiles></container>')
        z.writestr("c.opf", "<package><metadata></metadata></package>")
        z.writestr("ch.xhtml", "<html><body>" + "real book text " * 50 + "</body></html>")
    bd = tmp_path / "Good Book"; bd.mkdir(); f = bd / "b.epub"; f.write_bytes(buf.getvalue())
    work = Work(source_id=src.id, source_work_ref="stock:g", title="Good Book", status="complete",
                local_path=str(f), local_size=len(buf.getvalue()))
    db.add(work); db.commit(); db.refresh(work)
    si = StockItem(norm_key=cw.norm_key, catalog_work_id=cw.id, title="Good Book", status="stocked",
                   work_id=work.id, file_path=str(f))
    db.add(si); db.commit()
    out = stock_mod.sweep_integrity(db)
    assert out["corrupt"] == 0 and f.exists()
    db.refresh(si); assert si.status == "stocked"
