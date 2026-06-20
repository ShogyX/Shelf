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
    db.close()
