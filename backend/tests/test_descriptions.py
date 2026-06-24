"""Wave H: descriptions/synopses are normalized to plain text (no raw HTML/markdown reaches the UI)."""
from __future__ import annotations

from sqlalchemy import select, text

from app.db import SessionLocal, init_db, _backfill_descriptions, _DESC_BACKFILL_KEY
from app.models import AppSetting, CatalogGroup, CatalogWork, IndexedPage, Work
from app.textutil import clean_synopsis


def test_clean_synopsis_strips_markup_and_keeps_lone_markers():
    assert clean_synopsis("<p>A <i>boy</i></p><p>finds a library</p>") == "A boy\nfinds a library"
    assert clean_synopsis("Tom &amp; Jerry &#39;x&#39;") == "Tom & Jerry 'x'"
    assert clean_synopsis("**Bold** and _italic_ and [w](https://x.io)") == "Bold and italic and w"
    # NEGATIVE: a lone */_ (rating, filename, math) must survive untouched.
    assert clean_synopsis("Rated 4* and 5* — file_name_ok — 2 * 3") == "Rated 4* and 5* — file_name_ok — 2 * 3"
    assert clean_synopsis("") is None and clean_synopsis(None) is None


def test_work_and_catalog_validators_clean_on_assign():
    init_db()
    db = SessionLocal()
    w = Work(title="t", description="<p>Hello <b>world</b></p>")
    cw = CatalogWork(norm_key="k", title="t", work_url="u", domain="d", synopsis="**B** &amp; <i>x</i>")
    cg = CatalogGroup(norm_key="k2", media_bucket="text", title="t", synopsis="<br>line<br>two")
    ip = IndexedPage(site_id=1, url="u", description="<p>preview &amp; more</p>")
    assert w.description == "Hello world"
    assert cw.synopsis == "B & x"
    assert cg.synopsis == "line\ntwo"
    assert ip.description == "preview & more"
    db.close()


def test_backfill_cleans_existing_rows_and_is_guarded():
    init_db()
    db = SessionLocal()
    # Simulate legacy raw rows by writing past the validator via a Core UPDATE.
    w = Work(title="legacy", description="clean already")
    db.add(w); db.commit()
    db.execute(text("UPDATE works SET description = :d WHERE id = :i"),
               {"d": "<p>raw <b>html</b> here</p>", "i": w.id})
    # Make the backfill eligible to run again.
    db.execute(text("DELETE FROM app_settings WHERE key = :k"), {"k": _DESC_BACKFILL_KEY})
    db.commit(); db.close()

    _backfill_descriptions()

    db = SessionLocal()
    got = db.scalar(select(Work.description).where(Work.id == w.id))
    assert got == "raw html here"                                  # cleaned
    assert db.scalar(text("SELECT 1 FROM app_settings WHERE key = :k"), {"k": _DESC_BACKFILL_KEY})  # sentinel set
    db.delete(db.get(Work, w.id)); db.commit(); db.close()
