"""Operator blocklist + broken-content removal."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.db import SessionLocal, init_db
from app.ingestion import blocklist
from app.ingestion.engine import ComplianceError
from app.main import app
from app.models import CatalogWork, IndexBlock, IndexSite, User, Work


@pytest.fixture
def client_admin():
    init_db()
    db = SessionLocal()
    for model in (IndexBlock, CatalogWork, IndexSite):
        db.execute(delete(model))
    db.execute(delete(User))
    db.commit()
    db.close()
    with TestClient(app) as c:
        c.post("/api/auth/setup", json={"username": "admin", "password": "test1234"})
        yield c


# --------------------------------------------------------------- helper unit tests
def test_is_blocked_url_and_domain():
    init_db()
    db = SessionLocal()
    db.execute(delete(IndexBlock))
    db.commit()
    blocklist.add_block(db, scope="url", value="https://site.com/novel/x#frag")
    blocklist.add_block(db, scope="domain", value="https://bad.com/whatever")
    assert blocklist.is_blocked(db, "https://site.com/novel/x")        # exact (defragged)
    assert blocklist.is_blocked(db, "https://site.com/novel/x/")       # trailing slash
    assert not blocklist.is_blocked(db, "https://site.com/novel/y")    # different url
    assert blocklist.is_blocked(db, "https://bad.com/anything/here")   # domain match
    assert blocklist.is_blocked(db, "https://www.bad.com/x")           # www stripped
    assert not blocklist.is_blocked(db, "https://good.com/x")
    db.execute(delete(IndexBlock)); db.commit(); db.close()


def test_add_block_is_idempotent():
    init_db()
    db = SessionLocal()
    db.execute(delete(IndexBlock)); db.commit()
    a = blocklist.add_block(db, scope="url", value="https://s.com/a")
    b = blocklist.add_block(db, scope="url", value="https://s.com/a/")  # normalizes to same
    assert a.id == b.id
    assert len(db.scalars(select(IndexBlock)).all()) == 1
    db.execute(delete(IndexBlock)); db.commit(); db.close()


def test_hook_entry_refuses_blocked():
    import asyncio
    init_db()
    db = SessionLocal()
    db.execute(delete(IndexBlock)); db.commit()
    blocklist.add_block(db, scope="url", value="https://s.com/blocked-novel")
    entry = CatalogWork(provider="web_index", domain="s.com",
                        work_url="https://s.com/blocked-novel", norm_key="x", title="X")
    db.add(entry); db.commit(); db.refresh(entry)
    with pytest.raises(ComplianceError):
        asyncio.run(__import__("app.ingestion.catalog", fromlist=["hook_entry"]).hook_entry(db, entry))
    db.delete(entry); db.execute(delete(IndexBlock)); db.commit(); db.close()


# --------------------------------------------------------------- API flow tests
def _catalog(db, **kw):
    defaults = dict(provider="web_index", domain="ex.com", norm_key="k",
                    work_url="https://ex.com/w", title="W", health="unknown")
    defaults.update(kw)
    cw = CatalogWork(**defaults)
    db.add(cw); db.commit(); db.refresh(cw)
    return cw


def test_remove_catalog_blocks_and_keeps_library_work(client_admin):
    db = SessionLocal()
    # A hooked work + its catalog entry (broken).
    from app.models import Source
    src = db.scalar(select(Source).where(Source.key == "web_index"))
    if src is None:
        src = Source(key="web_index", display_name="web", adapter_key="web_index", tos_permitted=True)
        db.add(src); db.commit()
    w = Work(source_id=src.id, source_work_ref="blk-1", title="Broken", hooked=True, status="ongoing")
    db.add(w); db.commit(); db.refresh(w)
    cw = _catalog(db, work_url="https://ex.com/broken", health="incomplete", hooked_work_id=w.id)
    cid, wid, url = cw.id, w.id, cw.work_url
    db.close()

    r = client_admin.delete(f"/api/catalog/{cid}")
    assert r.status_code == 200
    body = r.json()
    assert body["deleted"] == cid and body["blocked"]["scope"] == "url"

    db = SessionLocal()
    assert db.get(CatalogWork, cid) is None          # index entry gone
    assert db.get(Work, wid) is not None              # library copy untouched (per design)
    assert blocklist.is_blocked(db, url)              # re-add barred
    db.close()


def test_remove_catalog_block_domain(client_admin):
    db = SessionLocal()
    cw = _catalog(db, work_url="https://spam.com/a", domain="spam.com")
    cid = cw.id
    db.close()
    r = client_admin.delete(f"/api/catalog/{cid}?block_domain=true")
    assert r.status_code == 200 and r.json()["blocked"]["scope"] == "domain"
    db = SessionLocal()
    assert blocklist.is_blocked(db, "https://spam.com/anything-else")
    db.close()


def test_remove_catalog_without_block(client_admin):
    db = SessionLocal()
    cw = _catalog(db, work_url="https://ex.com/keepok")
    cid, url = cw.id, cw.work_url
    db.close()
    r = client_admin.delete(f"/api/catalog/{cid}?block=false")
    assert r.status_code == 200 and r.json()["blocked"] is None
    db = SessionLocal()
    assert not blocklist.is_blocked(db, url)
    db.close()


def test_purge_broken_only_unhooked_broken(client_admin):
    db = SessionLocal()
    db.execute(delete(CatalogWork)); db.commit()
    _catalog(db, work_url="https://ex.com/b1", health="incomplete")
    _catalog(db, work_url="https://ex.com/b2", health="no_chapters")
    _catalog(db, work_url="https://ex.com/ok", health="ok")           # not broken → keep
    _catalog(db, work_url="https://ex.com/hooked", health="incomplete", hooked_work_id=1)  # hooked → keep
    db.close()
    r = client_admin.post("/api/catalog/purge-broken")
    assert r.status_code == 200 and r.json()["removed"] == 2
    db = SessionLocal()
    remaining = {c.work_url for c in db.scalars(select(CatalogWork)).all()}
    assert remaining == {"https://ex.com/ok", "https://ex.com/hooked"}
    assert blocklist.is_blocked(db, "https://ex.com/b1")
    db.close()


def test_block_management_endpoints(client_admin):
    r = client_admin.post("/api/index/blocks",
                          json={"scope": "domain", "value": "https://evil.com/x", "reason": "spam"})
    assert r.status_code == 200
    bid = r.json()["id"]
    assert r.json()["value"] == "evil.com"
    listed = client_admin.get("/api/index/blocks").json()
    assert any(b["id"] == bid for b in listed)
    assert client_admin.delete(f"/api/index/blocks/{bid}").status_code == 200
    db = SessionLocal()
    assert not blocklist.is_blocked(db, "https://evil.com/x")
    db.close()


def test_block_endpoints_require_admin():
    init_db()
    db = SessionLocal()
    db.execute(delete(User)); db.commit(); db.close()
    with TestClient(app) as c:
        c.post("/api/auth/setup", json={"username": "admin", "password": "test1234"})
        # create a normal user + log in as them
        c.post("/api/users", json={"username": "joe", "password": "test1234", "role": "user"})
        c.post("/api/auth/logout")
        c.post("/api/auth/login", json={"username": "joe", "password": "test1234"})
        assert c.post("/api/index/blocks", json={"scope": "url", "value": "https://x.com/a"}).status_code == 403
        assert c.delete("/api/catalog/1").status_code == 403
