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


def test_transcode_cache_hit(tmp_path, monkeypatch):
    """Non-native track → transcoded once, then reused from cache (no second ffmpeg run)."""
    src = tmp_path / "a.flac"
    src.write_bytes(b"x" * 10)
    monkeypatch.setattr(d, "_AUDIO_CACHE_DIR", str(tmp_path / "cache"))
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        with open(cmd[-1], "wb") as fh:   # ffmpeg writes to the trailing positional (the .part tmp)
            fh.write(b"transcoded")
        class R:  # noqa: D401
            returncode = 0
        return R()
    monkeypatch.setattr(d.subprocess, "run", fake_run)

    p1 = d._cached_transcode(1, 0, str(src))
    assert os.path.isfile(p1) and p1.endswith(".m4a") and len(calls) == 1
    p2 = d._cached_transcode(1, 0, str(src))
    assert p2 == p1 and len(calls) == 1   # cache hit — no re-transcode


def test_transcode_failure_cleans_partial(tmp_path, monkeypatch):
    """A failed ffmpeg run deletes the partial output and raises 409 (never serves a half file)."""
    import subprocess as sp
    from fastapi import HTTPException

    src = tmp_path / "a.flac"
    src.write_bytes(b"x")
    monkeypatch.setattr(d, "_AUDIO_CACHE_DIR", str(tmp_path / "c"))

    def boom(cmd, **kw):
        open(cmd[-1], "wb").close()        # leave a partial behind
        raise sp.CalledProcessError(1, cmd)
    monkeypatch.setattr(d.subprocess, "run", boom)

    with pytest.raises(HTTPException) as e:
        d._cached_transcode(2, 0, str(src))
    assert e.value.status_code == 409
    assert os.listdir(tmp_path / "c" / "2") == []   # partial cleaned up


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


def test_audio_stream_releases_db_connection_before_transcode(tmp_path, monkeypatch):
    """ROOT-CAUSE regression: the stream handler must RELEASE its pooled DB connection before the
    (up-to-30-min) ffmpeg transcode + file streaming. Holding it exhausted the pool (20+40) so every
    scheduler tick timed out with QueuePool TimeoutError → the ops.job_failed notification storm."""
    from fastapi.testclient import TestClient
    from sqlalchemy import delete
    from app.db import engine
    from app.main import app
    from app.models import User, UserSession

    init_db()
    db = SessionLocal()
    for m in (ReadingState, UserSession, User):
        db.execute(delete(m))
    db.commit()
    f = tmp_path / "bk.flac"        # non-native → forces the transcode path
    f.write_bytes(b"abcdefghij")
    w = Work(title="Audio Book", author="A", media_kind="audio", local_path=str(f))
    db.add(w); db.commit()
    wid = w.id
    db.close()

    monkeypatch.setattr(d, "_run_ffprobe", lambda p, **kw: {
        "format": {"duration": "60.0"},
        "streams": [{"codec_type": "audio", "codec_name": "flac"}]})   # flac = not native

    seen = {}
    out = tmp_path / "out.m4a"; out.write_bytes(b"transcoded")

    def fake_transcode(work_id, track, src):
        # The request's connection MUST already be back in the pool by the time the transcode runs.
        seen["checked_out"] = engine.pool.checkedout()
        return str(out)
    monkeypatch.setattr(d, "_cached_transcode", fake_transcode)

    with TestClient(app) as c:
        c.post("/api/auth/setup", json={"username": "admin", "password": "adminpw1"})
        r = c.get(f"/api/works/{wid}/audio/stream/0?transcode=1")
        assert r.status_code == 200, r.text
    assert seen["checked_out"] == 0, "DB connection was still held during the transcode"
