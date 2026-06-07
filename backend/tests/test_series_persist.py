"""Series persistence + annotation: prefer-hooked matching, DB tagging, and position stamping."""
from __future__ import annotations

from sqlalchemy import delete, select

from app.db import SessionLocal, init_db
from app.ingestion import series as S
from app.models import CatalogWork, Work


def _reset(db):
    db.execute(delete(Work))
    db.execute(delete(CatalogWork))
    db.commit()


def _row(nk, title, *, hooked=None, provider="hardcover"):
    return CatalogWork(provider=provider, provider_ref=f"r-{nk}-{hooked}", domain="d.app",
                       work_url=f"https://d.app/{nk}", norm_key=nk, title=title,
                       author="Terry Mancour", hooked_work_id=hooked)


def _book(nk, title, pos):
    return {"title": title, "author": "Terry Mancour", "year": 2014, "position": pos,
            "cover_url": None, "ref": f"hc:{nk}", "norm_key": nk}


def test_annotate_prefers_hooked_duplicate_row():
    db = SessionLocal()
    try:
        init_db(); _reset(db)
        w = Work(title="Hedgewitch", media_kind="text", status="complete")
        db.add(w); db.commit(); db.refresh(w)
        db.add(_row("hedgewitch", "Hedgewitch", hooked=None))        # duplicate unhooked listing
        db.add(_row("hedgewitch", "Hedgewitch", hooked=w.id))         # the owned one
        db.commit()
        out = S._annotate(db, "The Spellmonger", [_book("hedgewitch", "Hedgewitch", 14.0)])
        b = out["books"][0]
        assert b["hooked_work_id"] == w.id  # not masked by the unhooked duplicate
    finally:
        _reset(db); db.close()


def test_persist_tags_works_and_creates_missing_rows():
    db = SessionLocal()
    try:
        init_db(); _reset(db)
        w = Work(title="Warmage", media_kind="text", status="complete")
        db.add(w); db.commit(); db.refresh(w)
        db.add(_row("warmage", "Warmage", hooked=w.id))
        db.commit()
        books = [_book("warmage", "Warmage", 2.0), _book("necromancer", "Necromancer", 10.0)]
        S._persist_series(db, "The Spellmonger", books)
        # owned work got tagged with series + position
        db.refresh(w)
        assert w.series == "The Spellmonger" and w.series_position == 2.0
        # the hooked catalog row got the series tag too (so future imports carry position)
        hooked = db.scalar(select(CatalogWork).where(CatalogWork.norm_key == "warmage"))
        assert (hooked.extra or {}).get("series_position") == 2.0
        # a volume we don't own was created as a listing row carrying its position
        created = db.scalar(select(CatalogWork).where(CatalogWork.norm_key == "necromancer"))
        assert created is not None
        assert (created.extra or {}).get("series") == "The Spellmonger"
        assert (created.extra or {}).get("series_position") == 10.0
        assert (created.extra or {}).get("listing_only") is True
    finally:
        _reset(db); db.close()


def test_annotate_fallback_matches_owned_work_by_position():
    db = SessionLocal()
    try:
        init_db(); _reset(db)
        # An owned work tagged with series + position, but NO catalog row links to it.
        w = Work(title="Court Wizard (Spellmonger Series: Book 8)", media_kind="text",
                 status="complete", series="The Spellmonger", series_position=8.0)
        db.add(w); db.commit(); db.refresh(w)
        out = S._annotate(db, "The Spellmonger", [_book("court wizard", "Court Wizard", 8.0)])
        assert out["books"][0]["hooked_work_id"] == w.id  # matched via series+position fallback
    finally:
        _reset(db); db.close()
