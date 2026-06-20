"""The /works/{id}/audio download endpoint (Phase 3): single-file + multi-file audiobooks."""
from __future__ import annotations

import io
import zipfile

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.db import SessionLocal, init_db
from app.main import app
from app.models import LibraryItem, User, UserSession, Work


@pytest.fixture
def admin():
    init_db()
    db = SessionLocal()
    for m in (LibraryItem, UserSession, Work, User):
        db.execute(delete(m))
    db.commit()
    db.close()
    c = TestClient(app)
    c.post("/api/auth/setup", json={"username": "admin", "password": "test1234"})
    return c


def _audio_work(local_path: str) -> int:
    db = SessionLocal()
    w = Work(title="My Audiobook", media_kind="audio", local_path=local_path, status="complete")
    db.add(w)
    db.commit()
    db.refresh(w)
    uid = db.scalar(select(User.id).where(User.username == "admin"))
    db.add(LibraryItem(user_id=uid, work_id=w.id))
    db.commit()
    wid = w.id
    db.close()
    return wid


def test_single_file_audiobook_download(admin, tmp_path):
    f = tmp_path / "book.m4b"
    f.write_bytes(b"ID3audio-bytes" * 100)
    wid = _audio_work(str(f))
    r = admin.get(f"/api/works/{wid}/audio")
    assert r.status_code == 200
    assert r.content == f.read_bytes()
    # FileResponse RFC-5987-encodes a spaced filename (filename*=utf-8''My%20Audiobook.m4b).
    cd = r.headers.get("content-disposition", "")
    assert "attachment" in cd and ".m4b" in cd


def test_multi_file_audiobook_zips(admin, tmp_path):
    d = tmp_path / "abook"
    d.mkdir()
    (d / "ch01.mp3").write_bytes(b"\x00" * 512)
    (d / "ch02.mp3").write_bytes(b"\x01" * 512)
    (d / "cover.jpg").write_bytes(b"\xff" * 64)  # non-audio → excluded from the zip
    wid = _audio_work(str(d))
    r = admin.get(f"/api/works/{wid}/audio")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    assert sorted(names) == ["ch01.mp3", "ch02.mp3"]  # audio only, cover excluded


def test_non_audio_work_rejected(admin, tmp_path):
    db = SessionLocal()
    w = Work(title="A Novel", media_kind="text", status="complete")
    db.add(w)
    db.commit()
    db.refresh(w)
    uid = db.scalar(select(User.id).where(User.username == "admin"))
    db.add(LibraryItem(user_id=uid, work_id=w.id))
    db.commit()
    wid = w.id
    db.close()
    assert admin.get(f"/api/works/{wid}/audio").status_code == 409
