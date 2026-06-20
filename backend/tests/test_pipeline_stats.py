"""The Settings → Statistics pipeline-stats endpoint (aggregates jobs + ledger + web hooks)."""
from __future__ import annotations

from sqlalchemy import delete

from app.db import SessionLocal, init_db
from app.models import CatalogWork, ContentRequest, DownloadJob, Work
from app.routers.index import pipeline_stats


def _reset(db):
    for m in (DownloadJob, ContentRequest, CatalogWork, Work):
        db.execute(delete(m))
    db.commit()


def test_pipeline_stats_aggregates_routes_and_reasons():
    init_db()
    db = SessionLocal()
    _reset(db)
    # Downloads across routes (grab_kind → route): usenet=auto/stock, torrent, anna's=libgen, librivox.
    db.add_all([
        DownloadJob(title="a", grab_kind="auto", status="imported"),      # usenet success
        DownloadJob(title="b", grab_kind="stock", status="failed"),       # usenet fail
        DownloadJob(title="c", grab_kind="torrent", status="imported"),   # torrent success
        DownloadJob(title="d", grab_kind="torrent", status="downloading"),  # torrent in-flight
        DownloadJob(title="e", grab_kind="libgen", status="imported"),    # anna's success
        DownloadJob(title="f", grab_kind="librivox", status="failed"),    # librivox fail
    ])
    # Web-crawl hook (not a job) → a "web fetch" success.
    w = Work(title="Web Book")
    db.add(w)
    db.commit()
    db.refresh(w)
    db.add(CatalogWork(provider="web_index", domain="d", work_url="u", norm_key="web book",
                       title="Web Book", hooked_work_id=w.id))
    # Ledger: title-level outcomes + failure reasons.
    db.add_all([
        ContentRequest(norm_key="r1", title="r1", status="resolved"),
        ContentRequest(norm_key="r2", title="r2", status="unavailable", failure_reason="no_match"),
        ContentRequest(norm_key="r3", title="r3", status="unavailable", failure_reason="no_match"),
        ContentRequest(norm_key="r4", title="r4", status="unavailable", failure_reason="unverified"),
        ContentRequest(norm_key="r5", title="r5", status="open"),
    ])
    db.commit()

    d = pipeline_stats(db)
    routes = {r["route"]: r for r in d["downloads"]["by_route"]}
    assert routes["usenet"] == {"route": "usenet", "imported": 1, "failed": 1, "active": 0}
    assert routes["torrent"] == {"route": "torrent", "imported": 1, "failed": 0, "active": 1}
    assert routes["anna's archive"]["imported"] == 1
    assert routes["librivox"]["failed"] == 1
    assert d["downloads"]["totals"] == {"imported": 3, "failed": 2, "active": 1}
    assert d["web_fetch"]["hooked"] == 1
    assert d["requests"] == {"resolved": 1, "unavailable": 3, "open": 1, "searching": 0}
    reasons = {f["reason"]: f["count"] for f in d["failure_reasons"]}
    assert reasons == {"no_match": 2, "unverified": 1}
    assert all(f["label"] for f in d["failure_reasons"])  # every reason has a human label
    # Wave F: per-source search-queue + follow overview present (empty here).
    assert d["sources"] == {"by_source": [], "due_now": 0}
    assert d["following"] == {"authors": 0, "series": 0, "auto_added": 0}
    db.close()


def test_pipeline_stats_surfaces_source_queue_and_follows():
    from datetime import UTC, datetime, timedelta

    from app.models import Subscription, User, WorkSourceSearch
    init_db()
    db = SessionLocal()
    _reset(db)
    for m in (WorkSourceSearch, Subscription):
        db.execute(delete(m))
    cr = ContentRequest(norm_key="x", title="x", status="unavailable")
    db.add(cr); db.commit(); db.refresh(cr)
    now = datetime.now(UTC)
    db.add_all([
        WorkSourceSearch(content_request_id=cr.id, source="torrent", status="no_match"),     # searched
        WorkSourceSearch(content_request_id=cr.id, source="pipeline", status="unavailable",
                         next_retry_at=now - timedelta(minutes=1)),                            # queued + due
        WorkSourceSearch(content_request_id=cr.id, source="libgen", status="matched"),        # in flight
    ])
    u = User(username="f", password_hash="x", role="user"); db.add(u); db.commit(); db.refresh(u)
    db.add_all([
        Subscription(user_id=u.id, kind="author", key="a", display_name="A", auto_added=3),
        Subscription(user_id=u.id, kind="series", key="s", display_name="S", auto_added=1),
    ])
    db.commit()

    d = pipeline_stats(db)
    by_src = {s["source"]: s for s in d["sources"]["by_source"]}
    assert by_src["torrent"]["searched"] == 1
    assert by_src["pipeline"]["queued"] == 1
    assert by_src["libgen"]["in_flight"] == 1
    assert d["sources"]["due_now"] == 1
    assert d["following"] == {"authors": 1, "series": 1, "auto_added": 4}
    db.close()
