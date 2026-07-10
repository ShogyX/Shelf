"""Audiobooks are shared stock, not library items: they're surfaced as the 'listen' format of the
matching ebook (same normalized title) and access is gated on owning that ebook — see import_core
(no add_to_library), works.list_works (pairing + audio excluded), delivery._may_listen."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.db import SessionLocal, init_db
from app.main import app
from app.models import ContentRequest, ContentRequestRequester, LibraryItem, User, UserSession, Work


@pytest.fixture
def client():
    init_db()
    db = SessionLocal()
    # ContentRequest/Requester too: they're keyed by (norm_key, media_bucket), so a "dune" row left by
    # another test would collide with this suite's audiobook-request fixture and silently drop it.
    for m in (ContentRequestRequester, ContentRequest, LibraryItem, UserSession, Work, User):
        db.execute(delete(m))
    db.commit()
    db.close()
    c = TestClient(app)
    c.post("/api/auth/setup", json={"username": "admin", "password": "test1234"})
    c.post("/api/users", json={"username": "joe", "password": "test1234", "role": "user"})
    c.post("/api/users", json={"username": "bob", "password": "test1234", "role": "user"})
    return c


def _seed(tmp_path):
    """Joe owns the Dune EBOOK; a Dune AUDIOBOOK exists as a shared Work in NO library."""
    f = tmp_path / "dune.m4b"
    f.write_bytes(b"ID3" * 100)
    db = SessionLocal()
    ebook = Work(title="Dune", author="Frank Herbert", media_kind="text", status="complete")
    audio = Work(title="Dune", author="Frank Herbert", media_kind="audio",
                 local_path=str(f), status="complete")
    db.add_all([ebook, audio])
    db.commit()
    joe = db.scalar(select(User.id).where(User.username == "joe"))
    db.add(LibraryItem(user_id=joe, work_id=ebook.id))  # audio gets NO LibraryItem
    db.commit()
    ids = (ebook.id, audio.id)
    db.close()
    return ids


def test_audio_paired_and_excluded_and_access(client, tmp_path):
    ebook_id, audio_id = _seed(tmp_path)

    client.post("/api/auth/login", json={"username": "joe", "password": "test1234"})
    works = client.get("/api/works").json()
    # The shared audiobook is NOT a standalone library entry...
    assert [w["id"] for w in works] == [ebook_id]
    # ...it's surfaced as the ebook's 'listen' format.
    assert works[0]["audiobook_work_id"] == audio_id
    # Joe owns the matching ebook → may download the audiobook.
    assert client.get(f"/api/works/{audio_id}/audio").status_code == 200

    # Bob owns no matching ebook → 404 (can't even probe it exists).
    client.post("/api/auth/login", json={"username": "bob", "password": "test1234"})
    assert client.get("/api/works").json() == []
    assert client.get(f"/api/works/{audio_id}/audio").status_code == 404


def test_audiobook_requester_may_listen_without_ebook(client, tmp_path):
    """P8: a user who requested the AUDIOBOOK (a 'both' request where only the audio landed) can
    listen to it even without owning the matching ebook — delivery._may_listen honours the request."""
    from app.ingestion.extract import norm_title
    from app.models import ContentRequest, ContentRequestRequester

    _ebook_id, audio_id = _seed(tmp_path)
    db = SessionLocal()
    bob = db.scalar(select(User.id).where(User.username == "bob"))
    cr = ContentRequest(norm_key=norm_title("Dune"), media_bucket="audio", variant="audiobook",
                        title="Dune", status="resolved")
    db.add(cr); db.commit()
    db.add(ContentRequestRequester(request_id=cr.id, user_id=bob)); db.commit()
    db.close()

    client.post("/api/auth/login", json={"username": "bob", "password": "test1234"})
    # Bob still owns no ebook, so the audiobook stays out of his library grid...
    assert client.get("/api/works").json() == []
    # ...but because he REQUESTED the audiobook, he may now access/stream it (was 404 before P8).
    assert client.get(f"/api/works/{audio_id}/audio").status_code == 200
