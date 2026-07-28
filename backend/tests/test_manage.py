"""shelfmanage — the per-title maintenance commands.

These exist so an operator never has to hand-write SQL against the live library. That only holds if
they are SAFE by default, so the dry run is the property most worth pinning: every command must be
able to describe what it would do while changing nothing.
"""
from __future__ import annotations

import argparse
import os

import pytest
from sqlalchemy import delete, select

from app import manage
from app.db import SessionLocal, init_db
from app.models import Bookshelf, BookshelfItem, LibraryItem, Source, Work


@pytest.fixture
def clean():
    init_db()
    db = SessionLocal()
    for m in (BookshelfItem, Bookshelf, LibraryItem, Work):
        db.execute(delete(m))
    db.commit()
    if not db.scalar(select(Source)):
        db.add(Source(key="local", display_name="Local", adapter_key="local"))
        db.commit()
    db.close()
    yield


def _args(**kw):
    return argparse.Namespace(**{"yes": False, **kw})


def _audiobook(tmp_path, name="Winter's Heart", n=3):
    d = tmp_path / name
    d.mkdir()
    for i in range(1, n + 1):
        (d / f"{i:02d}.mp3").write_bytes(b"\0" * 2048)   # non-empty so it counts as playable
    return str(d)


def test_adopt_is_a_dry_run_by_default(clean, tmp_path, capsys):
    folder = _audiobook(tmp_path)
    assert manage.cmd_adopt_audiobook(_args(folder=folder, title="Winter's Heart",
                                            author="Robert Jordan", series=None,
                                            series_position=None)) == 0
    assert "dry run" in capsys.readouterr().out
    db = SessionLocal()
    try:
        assert db.scalar(select(Work)) is None, "dry run created a Work"
    finally:
        db.close()


def test_adopt_creates_the_work_and_is_idempotent(clean, tmp_path):
    folder = _audiobook(tmp_path)
    a = _args(folder=folder, title="Winter's Heart", author="Robert Jordan",
              series="The Wheel of Time", series_position=9, yes=True)
    assert manage.cmd_adopt_audiobook(a) == 0
    db = SessionLocal()
    try:
        w = db.scalar(select(Work))
        assert w.title == "Winter's Heart" and w.media_kind == "audio"
        assert w.local_path == folder and w.status == "complete"
        assert w.series == "The Wheel of Time" and w.series_position == 9
        first_id = w.id
    finally:
        db.close()
    # Re-running must not mint a second Work for the same folder.
    assert manage.cmd_adopt_audiobook(a) == 0
    db = SessionLocal()
    try:
        assert [x.id for x in db.scalars(select(Work)).all()] == [first_id]
    finally:
        db.close()


def test_adopt_refuses_a_folder_with_nothing_playable(clean, tmp_path, capsys):
    empty = tmp_path / "Nothing"
    empty.mkdir()
    (empty / "cover.jpg").write_bytes(b"x")
    (empty / "zero.mp3").write_bytes(b"")          # zero-byte doesn't count
    assert manage.cmd_adopt_audiobook(_args(folder=str(empty), title=None, author=None,
                                            series=None, series_position=None, yes=True)) == 2
    assert "no playable audio" in capsys.readouterr().err


def test_repoint_moves_the_path_and_drops_the_cached_manifest(clean, tmp_path):
    old, new = _audiobook(tmp_path, "Old"), _audiobook(tmp_path, "New")
    db = SessionLocal()
    try:
        w = Work(title="T", media_kind="audio", local_path=old,
                 audio_meta={"tracks": [{"index": i} for i in range(1582)]},
                 health="mismatch", health_detail="several books")
        db.add(w); db.commit(); wid = w.id
    finally:
        db.close()

    assert manage.cmd_repoint_work(_args(work_id=wid, path=new)) == 0   # dry run
    db = SessionLocal()
    try:
        assert db.get(Work, wid).local_path == old
    finally:
        db.close()

    assert manage.cmd_repoint_work(_args(work_id=wid, path=new, yes=True)) == 0
    db = SessionLocal()
    try:
        w = db.get(Work, wid)
        assert w.local_path == new
        assert w.audio_meta is None, "stale track manifest survived the repoint"
        assert w.health == "ok" and w.health_detail is None
    finally:
        db.close()


def test_remove_work_purges_back_pointers_but_keeps_files_by_default(clean, tmp_path):
    folder = _audiobook(tmp_path, "Doomed")
    db = SessionLocal()
    try:
        w = Work(title="Doomed", media_kind="audio", local_path=folder)
        db.add(w); db.commit(); wid = w.id
        db.add(LibraryItem(user_id=1, work_id=wid)); db.commit()
    finally:
        db.close()

    assert manage.cmd_remove_work(_args(work_id=wid, delete_files=False)) == 0   # dry run
    db = SessionLocal()
    try:
        assert db.get(Work, wid) is not None
    finally:
        db.close()

    assert manage.cmd_remove_work(_args(work_id=wid, delete_files=False, yes=True)) == 0
    db = SessionLocal()
    try:
        assert db.get(Work, wid) is None
        assert db.scalar(select(LibraryItem).where(LibraryItem.work_id == wid)) is None
    finally:
        db.close()
    assert os.path.isdir(folder), "files were deleted without --delete-files"


def test_unknown_work_id_is_an_error_not_a_traceback(clean, capsys):
    assert manage.cmd_remove_work(_args(work_id=999999, delete_files=False, yes=True)) == 2
    assert manage.cmd_repoint_work(_args(work_id=999999, path="/tmp", yes=True)) == 2
    assert "no work with id" in capsys.readouterr().err


def test_list_broken_reports_flagged_and_partially_damaged(clean, capsys):
    db = SessionLocal()
    try:
        db.add_all([
            Work(title="Gone", media_kind="audio", health="missing", health_detail="file missing"),
            Work(title="Partly", media_kind="audio", health="ok",
                 health_detail="1 of 20 track(s) unplayable"),
            Work(title="Fine", media_kind="audio", health="ok"),
        ])
        db.commit()
    finally:
        db.close()
    assert manage.cmd_list_broken(_args()) == 0
    out = capsys.readouterr().out
    assert "Gone" in out and "Partly" in out
    assert "Fine" not in out          # healthy titles are noise here
    assert "1 flagged, 1 partially damaged." in out
