"""Unit + router tests for the hybrid book catalog (network mocked)."""
from __future__ import annotations

import pytest
from sqlalchemy import delete, select

from app.db import SessionLocal, init_db
from app.ingestion import book_catalog as bc
from app.ingestion.catalog import media_label
from app.models import AppSetting, CatalogWork


def _reset(db):
    bc._resolve_seen.clear()  # module-global guard isn't cleared by the read-cache fixture
    db.execute(delete(CatalogWork))
    for k in (bc._CONFIG_KEY, bc._STATE_KEY):
        row = db.get(AppSetting, k)
        if row:
            db.delete(row)
    db.commit()


def _hit(source="openlibrary", ref="/works/OL1W", title="Project Hail Mary",
         author="Andy Weir", pop=1000.0, subjects=None):
    return bc.BookHit(source=source, ref=ref, title=title, author=author,
                      popularity=pop, url=f"https://x/{ref}", language="en",
                      subjects=subjects or ["science fiction"])


def test_closeness_scoring():
    init_db()
    db = SessionLocal(); _reset(db)
    bc.upsert_hit(db, _hit()); db.commit()
    rows = list(db.scalars(select(CatalogWork)).all())
    assert bc.closeness("Project Hail Mary", rows) == pytest.approx(1.0)
    assert bc.closeness("project hail mary", rows) >= 0.85  # contained → boosted
    assert bc.closeness("totally unrelated zzz qqq", rows) < 0.4
    assert bc.closeness("", rows) == 0.0
    db.close()


def test_upsert_hit_never_downgrades_comic_to_text():
    # A book provider re-seeding a row must NOT clobber an established comic back to text (the book
    # APIs shelve manga as books and return media_kind="text") — that oscillates the grouping bucket.
    init_db()
    db = SessionLocal(); _reset(db)
    e = bc.upsert_hit(db, _hit()); db.commit()
    e.media_kind = "comic"; db.commit()          # enrich tick / comix adapter flipped it to comic
    bc.upsert_hit(db, _hit(pop=3000.0)); db.commit()   # GB re-seed carries media_kind="text"
    db.refresh(e)
    assert e.media_kind == "comic"               # comic is sticky — not downgraded
    # But a NEW row still adopts the hit's text kind, and a hit that IS comic still upgrades.
    e.media_kind = "text"; db.commit()
    bc.upsert_hit(db, bc.BookHit(source="openlibrary", ref="/works/OL1W",
                                 title="Project Hail Mary", media_kind="comic")); db.commit()
    db.refresh(e)
    assert e.media_kind == "comic"               # text → comic upgrade still allowed
    db.close()


def test_upsert_creates_book_row_and_label():
    init_db()
    db = SessionLocal(); _reset(db)
    e = bc.upsert_hit(db, _hit()); db.commit()
    assert e is not None
    assert e.provider == "openlibrary" and e.integration_id is None
    assert e.domain == "openlibrary.org" and e.media_kind == "text"
    assert e.norm_key  # normalized
    assert media_label(e) == "Book"  # book providers label as Book, not Novel
    # genres derived from subjects → enriched stamped so the enrich tick skips it
    assert (e.extra or {}).get("genres")
    assert e.enriched_at is not None
    # idempotent upsert (same provider+ref) updates, doesn't duplicate
    bc.upsert_hit(db, _hit(pop=2000.0)); db.commit()
    assert db.scalar(select(CatalogWork).where(CatalogWork.provider == "openlibrary")) is not None
    assert len(list(db.scalars(select(CatalogWork)).all())) == 1
    db.close()


def test_upsert_sets_isbn_identity_key_so_same_book_merges():
    # MERGE-2: two rows from different providers carrying the same ISBN both get the deterministic
    # 'isbn:<isbn13>' identity_key, so the cross-source regroup pass merges them.
    init_db()
    db = SessionLocal(); _reset(db)
    a = bc.upsert_hit(db, bc.BookHit(source="googlebooks", ref="gb1", title="Project Hail Mary",
                                     author="Andy Weir", isbn=["0-306-40615-2"]))
    b = bc.upsert_hit(db, bc.BookHit(source="openlibrary", ref="ol1", title="Project Hail Mary",
                                     author="Andy Weir", isbn=["978-0-306-40615-7"]))
    db.commit()
    assert a.identity_key == "isbn:9780306406157"
    assert b.identity_key == a.identity_key  # ISBN-10 and ISBN-13 of the same book converge
    # first-id-wins: a later upsert with a different ISBN doesn't churn the key
    bc.upsert_hit(db, bc.BookHit(source="googlebooks", ref="gb1", title="Project Hail Mary",
                                 author="Andy Weir", isbn=["9781234567897"]))
    db.commit()
    assert a.identity_key == "isbn:9780306406157"
    db.close()


@pytest.mark.asyncio
async def test_resolve_live_upserts_and_caches(monkeypatch):
    init_db()
    db = SessionLocal(); _reset(db)
    calls = {"n": 0}

    async def fake_search_all(db_, query, *, limit):
        calls["n"] += 1
        return [_hit(source="openlibrary", ref="/works/OLa"),
                _hit(source="googlebooks", ref="gb1", title="Project Hail Mary")]

    monkeypatch.setattr(bc, "_search_all", fake_search_all)
    n = await bc.resolve_live(db, "Project Hail Mary")
    assert n == 2 and calls["n"] == 1
    # both providers persisted (they cluster later via regroup, but stay distinct rows)
    provs = {r.provider for r in db.scalars(select(CatalogWork)).all()}
    assert provs == {"openlibrary", "googlebooks"}
    # cached: a second resolve for the same query must NOT hit the network
    n2 = await bc.resolve_live(db, "Project Hail Mary")
    assert n2 == 0 and calls["n"] == 1
    db.close()


@pytest.mark.asyncio
async def test_resolve_if_sparse_gate(monkeypatch):
    init_db()
    db = SessionLocal(); _reset(db)
    ran = {"n": 0}

    async def fake_resolve(db_, query, *, limit=10):
        ran["n"] += 1
        return 1

    monkeypatch.setattr(bc, "resolve_live", fake_resolve)
    # Empty catalog → sparse → resolves.
    assert await bc.resolve_if_sparse(db, "Some Title") is True
    assert ran["n"] == 1
    # A close local row → gate passes, no resolve.
    bc.upsert_hit(db, _hit(title="The Hobbit", ref="/works/H")); db.commit()
    assert await bc.resolve_if_sparse(db, "The Hobbit") is False
    assert ran["n"] == 1  # unchanged
    db.close()


@pytest.mark.asyncio
async def test_sync_hot_set_advances_cursor(monkeypatch):
    init_db()
    db = SessionLocal(); _reset(db)

    async def fake_trending(client, period, *, limit=100):
        return [_hit(source="openlibrary", ref=f"/works/tr-{period}", title=f"Trend {period}")]

    async def fake_subject(client, subject, offset, *, limit=50):
        return [_hit(source="openlibrary", ref=f"/works/{subject}-{offset}", title=f"{subject} {offset}")]

    async def fake_gb(client, *, q, limit, key, start_index=0):
        return []

    monkeypatch.setattr(bc, "_ol_trending", fake_trending)
    monkeypatch.setattr(bc, "_ol_subject", fake_subject)
    monkeypatch.setattr(bc, "_gb_query", fake_gb)

    out = await bc.sync_hot_set(db, max_requests=2)
    assert out["added"] >= 1 and out["requests"] <= 2
    # cursor persisted so the next tick resumes
    st = bc._state(db)
    assert "cursor" in st
    db.close()


def test_config_get_set_defaults():
    init_db()
    db = SessionLocal(); _reset(db)
    assert bc.get_config(db) == bc._DEFAULTS
    bc.set_config(db, {"enabled": False, "hot_set_cap": 5000})
    cfg = bc.get_config(db)
    assert cfg["enabled"] is False and cfg["hot_set_cap"] == 5000
    assert cfg["closeness_threshold"] == bc._DEFAULTS["closeness_threshold"]  # untouched
    db.close()


# --- Router (admin-gated) ---
def test_book_catalog_router_admin_only(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.models import User

    init_db()
    db = SessionLocal()
    db.execute(delete(User)); db.execute(delete(CatalogWork)); db.commit(); db.close()

    async def fake_sync(db_, *, max_requests=8):
        return {"added": 0, "phase": "done"}

    monkeypatch.setattr(bc, "sync_hot_set", fake_sync)

    with TestClient(app) as admin:
        admin.post("/api/auth/setup", json={"username": "admin", "password": "test1234"})
        r = admin.get("/api/catalog/book-config")
        assert r.status_code == 200 and "config" in r.json()
        r = admin.put("/api/catalog/book-config", json={"closeness_threshold": 0.5})
        assert r.status_code == 200 and r.json()["config"]["closeness_threshold"] == 0.5
        assert admin.post("/api/catalog/book-sync").status_code == 200

        # admin creates a normal user
        assert admin.post(
            "/api/users", json={"username": "bob", "password": "test1234", "role": "user"}
        ).status_code == 200

        # A non-admin must be refused the admin config/sync endpoints (403, authenticated).
        with TestClient(app) as reader:
            reader.post("/api/auth/login", json={"username": "bob", "password": "test1234"})
            assert reader.get("/api/catalog/book-config").status_code == 403
            assert reader.post("/api/catalog/book-sync").status_code == 403
