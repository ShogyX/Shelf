"""Outbound-request telemetry: record → flush (upsert) → summary, with dedup/accumulate + pruning."""
from __future__ import annotations

from app import telemetry
from app.db import SessionLocal, init_db
from app.models import RequestStat
import sqlalchemy as sa


def _clear(db):
    db.execute(sa.delete(RequestStat))
    db.commit()
    telemetry.drain()  # discard any pending in-memory deltas from other tests


def test_record_flush_summary_roundtrip():
    init_db()
    db = SessionLocal()
    _clear(db)
    for _ in range(5):
        telemetry.record("openlibrary.org", "metadata")
    for _ in range(3):
        telemetry.record("comix.to", "crawl")
    telemetry.record("www.googleapis.com", "metadata")  # www. normalized away
    telemetry.record("x.com", "bogus-category")          # unknown category → "other"

    assert telemetry.flush(db) == 4                       # 4 distinct (bucket,host,category) groups
    s = telemetry.summary(db, hours=48)
    assert s["total"] == 10
    cats = {c["category"]: c["count"] for c in s["by_category"]}
    assert cats == {"metadata": 6, "crawl": 3, "other": 1}
    hosts = {h["host"]: h["count"] for h in s["by_host"]}
    assert hosts["openlibrary.org"] == 5 and hosts["googleapis.com"] == 1   # www stripped
    assert s["rates"]["per_day"] == 10 and s["series"]                      # has a time bucket
    # Each series bucket carries a per-category breakdown that sums to its total and
    # only references known categories.
    known = set(telemetry.CATEGORIES)
    for b in s["series"]:
        assert set(b["by_category"]).issubset(known)
        assert sum(b["by_category"].values()) == b["total"]
    # Across all buckets the per-category series sums to the top-level by_category totals.
    series_cat: dict[str, int] = {}
    for b in s["series"]:
        for cat, n in b["by_category"].items():
            series_cat[cat] = series_cat.get(cat, 0) + n
    assert series_cat == cats
    db.close()


def test_flush_accumulates_into_same_bucket_and_is_idempotent():
    init_db()
    db = SessionLocal()
    _clear(db)
    telemetry.record("comix.to", "crawl")
    telemetry.flush(db)
    assert telemetry.flush(db) == 0                       # nothing pending → no-op
    telemetry.record("comix.to", "crawl")
    telemetry.flush(db)                                   # same bucket+host+cat → count increments
    row = db.scalar(sa.select(RequestStat).where(RequestStat.host == "comix.to"))
    assert row.count == 2
    db.close()
