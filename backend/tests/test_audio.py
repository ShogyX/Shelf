"""Audiobook probe/manifest helpers (ffprobe mocked) + track-path safety."""
from __future__ import annotations

import os

import pytest

from app.db import SessionLocal, init_db
from app.models import ReadingState, Work
from app.routers import delivery as d


def _work(db, path):
    w = Work(title="T", author="A", media_kind="audio", local_path=path)
    db.add(w)
    db.commit()
    db.refresh(w)
    return w


def test_native_detection():
    assert d._native("aac") and d._native("mp3") and d._native("opus")
    assert not d._native("flac") and not d._native("wmav2") and not d._native(None)


def test_probe_single_file_chapters(tmp_path, monkeypatch):
    init_db()
    db = SessionLocal()
    f = tmp_path / "book.m4b"
    f.write_bytes(b"x")
    monkeypatch.setattr(d, "_run_ffprobe", lambda p, **kw: {
        "format": {"duration": "3600.0"},
        "streams": [{"codec_type": "audio", "codec_name": "aac"}],
        "chapters": [
            {"start_time": "0.0", "tags": {"title": "One"}},
            {"start_time": "1800.0", "tags": {"title": "Two"}},
        ],
    })
    w = _work(db, str(f))
    meta = d._probe_audio(db, w)
    assert meta and len(meta["tracks"]) == 1 and meta["tracks"][0]["native"] is True
    assert meta["total_duration_s"] == 3600.0
    assert [c["title"] for c in meta["chapters"]] == ["One", "Two"]
    assert meta["chapters"][1]["global_start_s"] == 1800.0
    # Second call must hit the cache (mtime unchanged) — re-probe would now blow up.
    def _boom(p):
        raise AssertionError("should have used the cache")
    monkeypatch.setattr(d, "_run_ffprobe", _boom)
    assert d._probe_audio(db, w)["total_duration_s"] == 3600.0
    db.close()


def test_probe_folder_tracks_and_path_safety(tmp_path, monkeypatch):
    init_db()
    db = SessionLocal()
    folder = tmp_path / "ab"
    folder.mkdir()
    for n in ("01.mp3", "02.mp3", "03.mp3"):
        (folder / n).write_bytes(b"x")
    monkeypatch.setattr(d, "_run_ffprobe", lambda p, **kw: {
        "format": {"duration": "100.0"},
        "streams": [{"codec_type": "audio", "codec_name": "mp3"}],
    })
    w = _work(db, str(folder))
    meta = d._probe_audio(db, w)
    assert len(meta["tracks"]) == 3 and len(meta["chapters"]) == 3
    assert meta["total_duration_s"] == 300.0
    assert [c["global_start_s"] for c in meta["chapters"]] == [0.0, 100.0, 200.0]
    # Track path resolves by sorted index, and an out-of-range index is rejected (404).
    assert os.path.basename(d._track_path(w, 0)) == "01.mp3"
    with pytest.raises(Exception):
        d._track_path(w, 9)
    db.close()


def test_audio_endpoints_integration(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from sqlalchemy import delete
    from app.main import app
    from app.models import User, UserSession

    init_db()
    db = SessionLocal()
    for m in (ReadingState, UserSession, User):
        db.execute(delete(m))
    db.commit()
    f = tmp_path / "bk.m4b"
    f.write_bytes(b"abcdefghij")
    w = Work(title="Audio Book", author="A", media_kind="audio", local_path=str(f))
    txt = Work(title="Txt", media_kind="text")
    db.add_all([w, txt])
    db.commit()
    wid, tid = w.id, txt.id
    db.close()

    monkeypatch.setattr(d, "_run_ffprobe", lambda p, **kw: {
        "format": {"duration": "120.0"},
        "streams": [{"codec_type": "audio", "codec_name": "aac"}],
        "chapters": [{"start_time": "0.0", "tags": {"title": "Intro"}},
                     {"start_time": "60.0", "tags": {"title": "Two"}}],
    })
    with TestClient(app) as c:
        c.post("/api/auth/setup", json={"username": "admin", "password": "adminpw1"})  # admin bypasses gate
        m = c.get(f"/api/works/{wid}/audio/manifest")
        assert m.status_code == 200, m.text
        body = m.json()
        assert body["total_duration_s"] == 120.0
        assert len(body["tracks"]) == 1 and body["tracks"][0]["native"] is True
        assert [ch["title"] for ch in body["chapters"]] == ["Intro", "Two"]
        assert body["tracks"][0]["url"].endswith(f"/works/{wid}/audio/stream/0")
        # Range request → 206 partial with the right content type (seek/stream works).
        s = c.get(f"/api/works/{wid}/audio/stream/0", headers={"Range": "bytes=0-3"})
        assert s.status_code == 206, s.status_code
        assert s.headers["content-type"] == "audio/mp4"
        # Progress round-trip + continue-listening surfaces it.
        assert c.post(f"/api/works/{wid}/audio/progress", json={"track": 0, "pos_s": 42.5}).status_code == 200
        assert c.get(f"/api/works/{wid}/audio/progress").json()["pos_s"] == 42.5
        cl = c.get("/api/continue-listening").json()
        assert any(it["work_id"] == wid and it["percent"] > 0 for it in cl)
        # A non-audio work is gated out (404, not 403).
        assert c.get(f"/api/works/{tid}/audio/manifest").status_code == 404
