"""LibriVox public-domain audiobook route (Phase 2)."""
from __future__ import annotations

import os

import pytest

import app.ingestion.adapters  # noqa: F401 — registers the local_folder adapter (_local_source)
from app import storage
from app.db import SessionLocal, init_db
from app.ingestion import librivox
from app.models import CatalogWork, DownloadJob, Work


def _book(title, lang="English", zip_url="u", first="Jane", last="Austen"):
    return {"title": title, "language": lang, "url_zip_file": zip_url,
            "authors": [{"first_name": first, "last_name": last}]}


def test_pick_best_match_and_author_bonus():
    books = [_book("Pride and Prejudice"), _book("Pride and Something Else")]
    b = librivox._pick(books, "Pride and Prejudice", "Jane Austen", "en")
    assert b is not None and b["title"] == "Pride and Prejudice"


def test_pick_language_gate_and_floor():
    # A French reading is excluded when the catalog work is English.
    assert librivox._pick([_book("Pride and Prejudice", lang="French")],
                          "Pride and Prejudice", "Jane Austen", "en") is None
    # A weak/unrelated title is below the confidence floor → no match.
    assert librivox._pick([_book("Totally Different Book")],
                          "Pride and Prejudice", None, "en") is None
    # Missing zip url → skipped.
    assert librivox._pick([_book("Pride and Prejudice", zip_url="")],
                          "Pride and Prejudice", None, "en") is None


@pytest.mark.asyncio
async def test_download_and_import_creates_audio_work(monkeypatch, tmp_path):
    init_db()
    db = SessionLocal()
    storage.set_audiobook_path(db, str(tmp_path / "audiobooks"))
    cw = CatalogWork(provider="openlibrary", provider_ref="/works/PP", domain="d", work_url="u",
                     title="Pride and Prejudice", author="Jane Austen", media_kind="text",
                     norm_key="pride and prejudice", language="en")
    db.add(cw)
    db.commit()
    db.refresh(cw)
    job = DownloadJob(catalog_work_id=cw.id, title="Pride and Prejudice", fmt="audio",
                      grab_kind="librivox", status="downloading")
    db.add(job)
    db.commit()
    db.refresh(job)
    jid = job.id

    async def fake_dl(url, staging):  # LibriVox-style concatenated-slug filename
        with open(os.path.join(staging, "prideandprejudice_01_austen_64kb.mp3"), "wb") as f:
            f.write(b"\x00" * 4096)
        return True
    monkeypatch.setattr(librivox, "_download_zip", fake_dl)
    # The staged file is a stub, not decodable audio — mock the structural check (codec-free test).
    monkeypatch.setattr("app.ingestion.verify.check_media_file", lambda p, k: (True, "ok"))

    await librivox._download_and_import(jid, "http://x/zip")

    db2 = SessionLocal()
    j = db2.get(DownloadJob, jid)
    assert j.status == "imported" and j.work_id
    w = db2.get(Work, j.work_id)
    assert w.media_kind == "audio" and w.title == "Pride and Prejudice"
    # Imported onto the separate audiobook path, NOT the ebook library.
    assert str(tmp_path / "audiobooks") in (w.local_path or "")
    db.close()
    db2.close()


@pytest.mark.asyncio
async def test_download_failure_marks_job_failed(monkeypatch, tmp_path):
    init_db()
    db = SessionLocal()
    storage.set_audiobook_path(db, str(tmp_path / "ab"))
    job = DownloadJob(title="X", fmt="audio", grab_kind="librivox", status="downloading")
    db.add(job)
    db.commit()
    db.refresh(job)
    jid = job.id

    async def fake_dl(url, staging):
        return False
    monkeypatch.setattr(librivox, "_download_zip", fake_dl)
    await librivox._download_and_import(jid, "http://x/zip")
    assert SessionLocal().get(DownloadJob, jid).status == "failed"
    db.close()
