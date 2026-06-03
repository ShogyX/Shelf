"""Boot recovery: a one-time normalization forces web_index to the unlimited daily budget, and
every boot re-queues pages that budget *pacing* (not a real failure) marked 'failed'."""
from __future__ import annotations

import pytest
from sqlalchemy import delete, select

from app.db import (
    _WEB_INDEX_UNLIMITED_KEY,
    SessionLocal,
    _recover_web_index_budget,
    _remove_retired_sources,
    init_db,
)
from app.ingestion import adapters  # noqa: F401 — registers built-in adapters (web_index)
from app.ingestion.adapters.web_index import WebIndexAdapter
from app.ingestion.base import registry
from app.ingestion.engine import ensure_source
from app.models import AppSetting, IndexedPage, IndexSite, Source, Work

NEW_BUDGET = WebIndexAdapter.compliance.max_daily_requests  # 0 = unlimited


@pytest.fixture
def db():
    init_db()
    s = SessionLocal()
    # Order matters: Work FKs Source. Drop any leftover retired (mangadex) source too, and the
    # one-time sentinel so each test exercises the forced-normalization path from a clean slate.
    for m in (IndexedPage, IndexSite, Work):
        s.execute(delete(m))
    s.execute(delete(Source).where(Source.key == "mangadex"))
    s.execute(delete(AppSetting).where(AppSetting.key == _WEB_INDEX_UNLIMITED_KEY))
    s.commit()
    yield s
    s.close()


def _seed(db, *, budget):
    src = ensure_source(db, registry.get("web_index"))
    src.max_daily_requests = budget
    site = IndexSite(root_url="https://x.com", domain="x.com", status="done")
    db.add(site)
    db.commit()
    db.refresh(site)
    db.add_all([
        IndexedPage(site_id=site.id, url="https://x.com/budget", status="failed", attempts=1,
                    last_error="daily budget of 2000 requests exhausted"),
        IndexedPage(site_id=site.id, url="https://x.com/dead", status="failed", attempts=5,
                    last_error="HTTP 404"),
    ])
    db.commit()
    return src.id, site.id


def test_recover_forces_unlimited_and_requeues_budget_failures(db):
    # Any positive cap is normalized to unlimited (0) on the one-time pass, and budget-stranded
    # pages are recovered while genuine failures (404) are left alone.
    _seed(db, budget=2000)
    db.close()

    _recover_web_index_budget()

    s = SessionLocal()
    src = s.scalar(select(Source).where(Source.key == "web_index"))
    pages = {p.url: p for p in s.scalars(select(IndexedPage)).all()}
    sentinel = s.get(AppSetting, _WEB_INDEX_UNLIMITED_KEY)
    s.close()
    assert src.max_daily_requests == NEW_BUDGET == 0       # forced to unlimited
    assert sentinel is not None                            # one-time pass recorded
    assert pages["https://x.com/budget"].status == "pending"
    assert pages["https://x.com/budget"].attempts == 0
    assert pages["https://x.com/budget"].last_error is None
    assert pages["https://x.com/dead"].status == "failed"


def test_recover_respects_operator_cap_after_one_time_pass(db):
    # Once the one-time normalization has run (sentinel present), a cap the operator sets later
    # is NOT overwritten on subsequent boots.
    _seed(db, budget=7777)
    db.add(AppSetting(key=_WEB_INDEX_UNLIMITED_KEY, value={"done": True}))
    db.commit()
    db.close()

    _recover_web_index_budget()

    s = SessionLocal()
    src = s.scalar(select(Source).where(Source.key == "web_index"))
    s.close()
    assert src.max_daily_requests == 7777


def test_remove_retired_sources_drops_orphan_mangadex(db):
    # A leftover retired-adapter Source row (mangadex) with no works is removed on boot.
    db.add(Source(key="mangadex", display_name="MangaDex", adapter_key="mangadex",
                  license_basis="user-attested", max_daily_requests=6000))
    db.commit()
    db.close()

    _remove_retired_sources()

    s = SessionLocal()
    gone = s.scalar(select(Source).where(Source.key == "mangadex"))
    s.close()
    assert gone is None


def test_init_db_is_data_safe_for_clients(db):
    """init_db is schema-only and is called by read-only clients (shelfcli) on every start — it
    must NOT run the data recoveries (budget normalize / retired-source delete / WAL checkpoint),
    which would write to the live server DB and lock the client. boot_recover() does that work."""
    src = ensure_source(db, registry.get("web_index"))
    src.max_daily_requests = 2000
    db.add(Source(key="mangadex", display_name="MangaDex", adapter_key="mangadex",
                  license_basis="user-attested", max_daily_requests=6000))
    db.commit()
    db.close()

    init_db()  # schema-only: must leave the seeded data untouched

    s = SessionLocal()
    wi = s.scalar(select(Source).where(Source.key == "web_index"))
    md = s.scalar(select(Source).where(Source.key == "mangadex"))
    sentinel = s.get(AppSetting, _WEB_INDEX_UNLIMITED_KEY)
    s.close()
    assert wi.max_daily_requests == 2000   # NOT normalized by init_db
    assert md is not None                  # mangadex NOT removed by init_db
    assert sentinel is None                # recovery did not run


def test_boot_recover_runs_the_data_recoveries(db):
    """The server-only boot path DOES normalize the budget, drop retired sources, and set the
    sentinel — the work moved out of init_db."""
    from app.db import boot_recover

    src = ensure_source(db, registry.get("web_index"))
    src.max_daily_requests = 2000
    db.add(Source(key="mangadex", display_name="MangaDex", adapter_key="mangadex",
                  license_basis="user-attested", max_daily_requests=6000))
    db.commit()
    db.close()

    boot_recover()

    s = SessionLocal()
    wi = s.scalar(select(Source).where(Source.key == "web_index"))
    md = s.scalar(select(Source).where(Source.key == "mangadex"))
    sentinel = s.get(AppSetting, _WEB_INDEX_UNLIMITED_KEY)
    s.close()
    assert wi.max_daily_requests == NEW_BUDGET == 0  # normalized to unlimited
    assert md is None                                 # retired source removed
    assert sentinel is not None


def test_remove_retired_sources_keeps_referenced_source(db):
    # If a Work still references the retired source, it's left in place (don't orphan content).
    from app.models import Work

    src = Source(key="mangadex", display_name="MangaDex", adapter_key="mangadex",
                 license_basis="user-attested", max_daily_requests=6000)
    db.add(src)
    db.commit()
    db.refresh(src)
    db.add(Work(source_id=src.id, source_work_ref="x", title="Kept", language="en",
                status="ongoing"))
    db.commit()
    db.close()

    _remove_retired_sources()

    s = SessionLocal()
    still = s.scalar(select(Source).where(Source.key == "mangadex"))
    s.close()
    assert still is not None
