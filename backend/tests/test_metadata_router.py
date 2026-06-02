"""End-to-end: metadata-provider router (admin-gated) over a TestClient."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.db import SessionLocal, init_db
from app.main import app
from app.models import MetadataLink, QueuedHook, Source, User, Work


@pytest.fixture
def client_admin():
    init_db()
    db = SessionLocal()
    for model in (MetadataLink, QueuedHook):
        db.execute(delete(model))
    db.execute(delete(User))
    db.commit()
    db.close()
    with TestClient(app) as c:
        c.post("/api/auth/setup", json={"username": "admin", "password": "test1234"})
        yield c


def _work_with_link(db):
    src = db.scalar(select(Source).where(Source.key == "generic_feed"))
    if src is None:
        src = Source(key="generic_feed", display_name="gf", adapter_key="generic_feed",
                     tos_permitted=True)
        db.add(src); db.commit()
    w = Work(source_id=src.id, source_work_ref="router-meta-1", title="Parent Work",
             hooked=True, status="ongoing", author="A")
    db.add(w); db.commit(); db.refresh(w)
    link = MetadataLink(work_id=w.id, provider="ranobedb", ref="900", matched_title="Parent Work",
                        confidence=0.9, status="auto", total_units=5, unit_kind="volumes",
                        payload={"url": "https://ranobedb.org/series/900", "status": "ongoing",
                                 "related": [{"title": "Side Story Z", "relation": "spin-off",
                                              "ref": "901"}]})
    db.add(link); db.commit(); db.refresh(w); db.refresh(link)
    return w.id, link.id


def test_metadata_endpoints_flow(client_admin):
    db = SessionLocal()
    work_id, link_id = _work_with_link(db)
    db.close()

    # Links surface for the work.
    r = client_admin.get(f"/api/works/{work_id}/metadata")
    assert r.status_code == 200 and r.json()[0]["provider"] == "ranobedb"

    # Related titles surface, not yet queued / not in library.
    r = client_admin.get(f"/api/works/{work_id}/related")
    assert r.status_code == 200
    rel = r.json()["related"]
    assert rel and rel[0]["title"] == "Side Story Z" and rel[0]["queued_status"] is None

    # Queue the related title.
    r = client_admin.post(f"/api/works/{work_id}/queue-related")
    assert r.status_code == 200 and r.json()["queued"] == 1

    # Now it shows as queued, and appears in the hook queue.
    rel = client_admin.get(f"/api/works/{work_id}/related").json()["related"]
    assert rel[0]["queued_status"] == "pending"
    hooks = client_admin.get("/api/queued-hooks").json()
    assert any(h["title"] == "Side Story Z" and h["status"] == "pending" for h in hooks)

    # Confirm the link locks its status.
    r = client_admin.post(f"/api/metadata-links/{link_id}/confirm")
    assert r.status_code == 200 and r.json()["status"] == "confirmed"

    # Process queue (nothing in the index yet) → hooked 0, stays pending.
    r = client_admin.post("/api/queued-hooks/process")
    assert r.status_code == 200 and r.json()["hooked"] == 0

    # Clean up via the delete endpoints.
    qid = client_admin.get("/api/queued-hooks").json()[0]["id"]
    assert client_admin.delete(f"/api/queued-hooks/{qid}").status_code == 200
    assert client_admin.delete(f"/api/metadata-links/{link_id}").status_code == 200
