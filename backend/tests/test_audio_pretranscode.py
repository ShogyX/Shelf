"""The audio pre-transcode warmer must not retry tracks that can never succeed.

A single mis-imported audiobook folder with 256 zero-byte files had this warmer spawning ffmpeg for
each of them on every tick — ~74k failed runs and full tracebacks a day in production. Besides the
log noise it consumed the per-tick time budget, so tracks that WOULD have warmed never got a turn.
"""
from __future__ import annotations

import asyncio
import os

from sqlalchemy import delete

from app.db import SessionLocal, init_db
from app.ingestion.scheduler import audio_pretranscode_tick
from app.models import Work


def _audio_work(db, tmp_path, *, size: int) -> tuple[Work, str]:
    src = tmp_path / "track1.wma"          # non-native → the warmer wants to transcode it
    src.write_bytes(b"\0" * size)
    work = Work(title="Broken Audiobook", media_kind="audio", language="en",
                hooked=False, local_path=str(src))
    db.add(work)
    db.commit()
    return work, str(src)


def _run(monkeypatch, work, src, cache_dir, *, calls: list):
    """Run one tick with the transcode + probe stubbed, recording every transcode attempt."""
    import app.routers.delivery as delivery

    monkeypatch.setattr(delivery, "_AUDIO_CACHE_DIR", str(cache_dir))
    monkeypatch.setattr(delivery, "_track_path", lambda w, i: src)
    monkeypatch.setattr(delivery, "_probe_audio",
                        lambda db, w: {"tracks": [{"index": 1, "native": False}]})

    def _boom(work_id, track, source):
        calls.append((work_id, track))
        raise RuntimeError("ffmpeg said no")

    monkeypatch.setattr(delivery, "_cached_transcode", _boom)
    asyncio.run(audio_pretranscode_tick())   # the @scheduled_task wrapper owns the session


def test_zero_byte_source_never_spawns_ffmpeg(tmp_path, monkeypatch):
    """The exact production case: an empty file can't transcode, so don't even try."""
    init_db()
    db = SessionLocal()
    db.execute(delete(Work))
    db.commit()
    work, src = _audio_work(db, tmp_path, size=0)
    db.close()

    calls: list = []
    for _ in range(3):                      # three ticks
        _run(monkeypatch, work, src, tmp_path / "cache", calls=calls)
    assert calls == [], "spawned a transcode for a zero-byte source"


def test_a_failing_track_is_not_retried_every_tick(tmp_path, monkeypatch):
    """A non-empty but unreadable file is attempted ONCE, then remembered until the source changes."""
    init_db()
    db = SessionLocal()
    db.execute(delete(Work))
    db.commit()
    work, src = _audio_work(db, tmp_path, size=1024)
    db.close()

    cache = tmp_path / "cache"
    calls: list = []
    for _ in range(5):                      # five ticks
        _run(monkeypatch, work, src, cache, calls=calls)
    assert len(calls) == 1, f"retried a doomed track every tick ({len(calls)} attempts)"

    # Replacing the file (new mtime) clears the memo, so a repaired file is picked up again.
    os.utime(src, (os.path.getmtime(src) + 500, os.path.getmtime(src) + 500))
    _run(monkeypatch, work, src, cache, calls=calls)
    assert len(calls) == 2, "a replaced source file was not retried"
