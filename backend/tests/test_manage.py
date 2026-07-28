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
            # health_detail also carries BENIGN status; it must not be read as damage. Reporting
            # every 'ok' work that merely had a detail listed 842 healthy titles as damaged.
            Work(title="Crawled", media_kind="text", health="ok",
                 health_detail="All discovered chapters fetched."),
        ])
        db.commit()
    finally:
        db.close()
    assert manage.cmd_list_broken(_args()) == 0
    out = capsys.readouterr().out
    assert "Gone" in out and "Partly" in out
    assert "Fine" not in out          # healthy titles are noise here
    assert "Crawled" not in out       # a benign detail is not damage
    assert "1 flagged, 1 partially damaged." in out


# --------------------------------------------------------------------------------- heal-hooks
def _hooked(db, cw_title, work_title, gid=None):
    from app.models import CatalogGroup, CatalogWork, Work
    w = Work(title=work_title, media_kind="text")
    db.add(w); db.commit(); db.refresh(w)
    cw = CatalogWork(provider="openlibrary", provider_ref=f"r{w.id}", domain="d",
                     work_url=f"u{w.id}", title=cw_title, media_kind="text",
                     hooked_work_id=w.id, group_id=gid)
    db.add(cw); db.commit()
    return cw, w


def test_heal_hooks_clears_unrelated_hooks_but_spares_edition_drift(clean, tmp_path):
    """The heal must be conservative: a wrong hook left in place is recoverable, but clearing a
    CORRECT one makes an owned title look unacquired."""
    from app import manage
    from app.models import CatalogWork

    db = SessionLocal()
    try:
        wrong, _ = _hooked(db, "War and Peace", "One Piece")
        wrong2, _ = _hooked(db, "Charlie and the Chocolate Factory", "Tales Of Demons And Gods")
        edition, _ = _hooked(db, "Dubliners", "Dubliners (Oxford World's Classics)")
        spelling, _ = _hooked(db, "The Island of Dr. Moreau", "The Island of Doctor Moreau")
        subtitle, _ = _hooked(db, "A Wizard of Earthsea", "A Wizard of Earthsea (The Earthsea Cycle)")
        ids = {"wrong": wrong.id, "wrong2": wrong2.id, "edition": edition.id,
               "spelling": spelling.id, "subtitle": subtitle.id}
    finally:
        db.close()

    assert manage.cmd_heal_hooks(_args(show=5)) == 0          # dry run
    db = SessionLocal()
    try:
        assert db.get(CatalogWork, ids["wrong"]).hooked_work_id is not None, "dry run cleared a hook"
    finally:
        db.close()

    assert manage.cmd_heal_hooks(_args(show=5, yes=True)) == 0
    db = SessionLocal()
    try:
        assert db.get(CatalogWork, ids["wrong"]).hooked_work_id is None
        assert db.get(CatalogWork, ids["wrong2"]).hooked_work_id is None
        for k in ("edition", "spelling", "subtitle"):
            assert db.get(CatalogWork, ids[k]).hooked_work_id is not None, f"cleared a correct hook: {k}"
    finally:
        db.close()

    # Re-runnable: a second pass finds nothing left to do.
    assert manage.cmd_heal_hooks(_args(show=5, yes=True)) == 0


def test_heal_hooks_ignores_a_shared_volume_number(clean):
    """'Heartstopper: Volume Four' and 'Skysworn: Cradle: Volume Four' share only structural words —
    that must not read as the same title."""
    from app import manage
    from app.models import CatalogWork

    db = SessionLocal()
    try:
        cw, _ = _hooked(db, "Heartstopper: Volume Four", "Skysworn: Cradle: Volume Four")
        cid = cw.id
    finally:
        db.close()
    assert manage.cmd_heal_hooks(_args(show=3, yes=True)) == 0
    db = SessionLocal()
    try:
        assert db.get(CatalogWork, cid).hooked_work_id is None
    finally:
        db.close()


def test_heal_hooks_spares_a_cross_script_edition(clean):
    """A Greek/Japanese edition row legitimately shares no word with its English work, so word
    overlap can't judge it. Spare those rather than risk clearing a real translation."""
    from app import manage
    from app.models import CatalogWork

    db = SessionLocal()
    try:
        cw, _ = _hooked(db, "Ἰλιάς", "The Iliad")
        cid = cw.id
    finally:
        db.close()
    assert manage.cmd_heal_hooks(_args(show=3, yes=True)) == 0
    db = SessionLocal()
    try:
        assert db.get(CatalogWork, cid).hooked_work_id is not None, "cleared a cross-script edition"
    finally:
        db.close()


def test_heal_hooks_spares_a_hook_the_file_path_vindicates(clean, tmp_path):
    """A Work's TITLE is often a filename-ish stub ("Ian Fleming - Bond 1") whose hook from the real
    title ("Casino Royale") is correct. The path still carries the title — check it before clearing."""
    from app import manage
    from app.models import CatalogWork, Work

    db = SessionLocal()
    try:
        good = tmp_path / "Casino Royale"
        good.mkdir()
        f = good / "James Bond 01 - Ian Fleming - Casino Royale.mobi"
        f.write_bytes(b"x")
        w = Work(title="Ian Fleming - Bond 1", media_kind="text", local_path=str(f))
        db.add(w); db.commit(); db.refresh(w)
        cw = CatalogWork(provider="openlibrary", provider_ref="rb", domain="d", work_url="ub",
                         title="Casino Royale", media_kind="text", hooked_work_id=w.id)
        db.add(cw); db.commit()
        cid = cw.id
        # ...while a genuinely wrong hook whose path says nothing is still cleared.
        w2 = Work(title="One Piece", media_kind="text", local_path=str(tmp_path / "One Piece.epub"))
        db.add(w2); db.commit(); db.refresh(w2)
        cw2 = CatalogWork(provider="openlibrary", provider_ref="rw", domain="d", work_url="uw",
                          title="War and Peace", media_kind="text", hooked_work_id=w2.id)
        db.add(cw2); db.commit()
        cid2 = cw2.id
    finally:
        db.close()

    assert manage.cmd_heal_hooks(_args(show=3, yes=True)) == 0
    db = SessionLocal()
    try:
        assert db.get(CatalogWork, cid).hooked_work_id is not None, "cleared a path-vindicated hook"
        assert db.get(CatalogWork, cid2).hooked_work_id is None
    finally:
        db.close()
