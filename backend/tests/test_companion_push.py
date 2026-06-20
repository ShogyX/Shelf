"""Push stocked content to companion apps (Phase 2): Storyteller copy+create+align, ABS scan nudge."""
from __future__ import annotations

import os

import pytest
from sqlalchemy import delete, select

from app.db import SessionLocal, init_db
from app.ingestion import companion
from app.models import CompanionPush, Integration, StockItem, Work


def _reset(db):
    for m in (CompanionPush, StockItem, Work, Integration):
        db.execute(delete(m))
    db.commit()


def _stock_work(db, title, norm, kind, path):
    w = Work(title=title, media_kind=kind, local_path=path, status="complete")
    db.add(w)
    db.commit()
    db.refresh(w)
    # Ebooks are tracked by a StockItem; audiobooks (media_kind="audio") are found directly (the
    # StockItem norm_key is unique, so a title can't have both as stock items).
    if kind != "audio":
        db.add(StockItem(norm_key=norm, title=title, status="stocked", work_id=w.id,
                         media_label="Book", media_category="Book"))
        db.commit()
    return w


@pytest.mark.asyncio
async def test_push_storyteller_copies_creates_and_aligns(monkeypatch, tmp_path):
    init_db()
    db = SessionLocal()
    _reset(db)
    epub = tmp_path / "book.epub"
    epub.write_bytes(b"PK\x03\x04 fake-epub")
    audio = tmp_path / "book.m4b"
    audio.write_bytes(b"\x00" * 1024)
    we = _stock_work(db, "My Book", "my book", "text", str(epub))
    wa = _stock_work(db, "My Book", "my book", "audio", str(audio))
    import_root = tmp_path / "st_import"
    integ = Integration(kind="storyteller", name="ST", base_url="http://h", api_key="pw",
                        config={"username": "u", "import_path": str(import_root)})
    db.add(integ)
    db.commit()
    db.refresh(integ)

    calls = {"created": [], "processed": []}

    class Fake:
        async def create_book(self, paths, collection=None):
            calls["created"].append(list(paths))
            return {"uuid": f"bk{len(calls['created'])}"}

        async def process(self, book_id):
            calls["processed"].append(book_id)
    monkeypatch.setattr(companion, "client_for", lambda i: Fake())

    res = await companion._push_storyteller(db, integ)
    assert res["created"] == 2 and res["aligned"] == 1
    # Both formats recorded as pushed, then aligned (both halves present).
    pushes = {p.fmt: p for p in db.scalars(select(CompanionPush)).all()}
    assert set(pushes) == {"ebook", "audio"}
    assert all(p.status == "aligned" and p.external_ref for p in pushes.values())
    # Files COPIED (originals untouched) into import_path/<title>/.
    assert (import_root / "My Book" / "book.epub").exists()
    assert (import_root / "My Book" / "book.m4b").exists()
    assert epub.exists() and audio.exists()  # originals not moved
    assert calls["processed"]  # alignment triggered

    # Idempotent: a second push creates nothing new.
    res2 = await companion._push_storyteller(db, integ)
    assert res2["created"] == 0


@pytest.mark.asyncio
async def test_push_one_format_defers_alignment_until_both(monkeypatch, tmp_path):
    # An ebook-only title must push but NOT align (status stays 'pushed'), so when its audiobook
    # arrives in a later tick alignment can finally fire on both halves.
    init_db()
    db = SessionLocal()
    _reset(db)
    epub = tmp_path / "b.epub"
    epub.write_bytes(b"PK\x03\x04")
    _stock_work(db, "Solo", "solo", "text", str(epub))
    import_root = tmp_path / "imp"
    integ = Integration(kind="storyteller", name="ST", base_url="http://h", api_key="pw",
                        config={"username": "u", "import_path": str(import_root)})
    db.add(integ)
    db.commit()
    db.refresh(integ)

    calls = {"processed": []}

    class Fake:
        async def create_book(self, paths, collection=None):
            return {"uuid": "shared-uuid"}

        async def process(self, book_id):
            calls["processed"].append(book_id)
    monkeypatch.setattr(companion, "client_for", lambda i: Fake())

    r1 = await companion._push_storyteller(db, integ)
    assert r1["created"] == 1 and r1["aligned"] == 0 and not calls["processed"]
    p = db.scalar(select(CompanionPush))
    assert p.status == "pushed"  # NOT prematurely aligned

    # The audiobook arrives → next tick aligns both halves.
    audio = tmp_path / "b.m4b"
    audio.write_bytes(b"\x00" * 16)
    _stock_work(db, "Solo", "solo", "audio", str(audio))
    r2 = await companion._push_storyteller(db, integ)
    assert r2["created"] == 1 and r2["aligned"] == 1 and calls["processed"] == ["shared-uuid"]


@pytest.mark.asyncio
async def test_transient_create_failure_retries_next_tick(monkeypatch, tmp_path):
    # A transient Storyteller failure must NOT write a permanent 'failed' row (which would block the
    # push forever) — it retries and succeeds on the next tick.
    from app.integrations.base import IntegrationError
    init_db()
    db = SessionLocal()
    _reset(db)
    epub = tmp_path / "b.epub"
    epub.write_bytes(b"PK\x03\x04")
    _stock_work(db, "Flaky", "flaky", "text", str(epub))
    integ = Integration(kind="storyteller", name="ST", base_url="http://h", api_key="pw",
                        config={"username": "u", "import_path": str(tmp_path / "imp")})
    db.add(integ)
    db.commit()
    db.refresh(integ)

    state = {"fail": True}

    class Fake:
        async def create_book(self, paths, collection=None):
            if state["fail"]:
                raise IntegrationError("storyteller momentarily down")
            return {"uuid": "ok"}

        async def process(self, book_id):
            pass
    monkeypatch.setattr(companion, "client_for", lambda i: Fake())

    r1 = await companion._push_storyteller(db, integ)
    assert r1["created"] == 0
    assert db.query(CompanionPush).count() == 0  # NOT recorded as failed → not poisoned

    state["fail"] = False
    r2 = await companion._push_storyteller(db, integ)
    assert r2["created"] == 1  # retried and succeeded


@pytest.mark.asyncio
async def test_push_storyteller_needs_import_path(monkeypatch):
    init_db()
    db = SessionLocal()
    _reset(db)
    integ = Integration(kind="storyteller", name="ST", base_url="http://h", api_key="pw",
                        config={"username": "u"})  # no import_path
    db.add(integ)
    db.commit()
    db.refresh(integ)
    res = await companion._push_storyteller(db, integ)
    assert "error" in res and integ.last_error


@pytest.mark.asyncio
async def test_push_storyteller_converts_non_epub(monkeypatch, tmp_path):
    if not companion.convert._has_calibre():
        pytest.skip("calibre not installed")
    init_db()
    db = SessionLocal()
    _reset(db)
    txt = tmp_path / "book.txt"
    txt.write_text("Chapter One\n\n" + ("words " * 200))
    _stock_work(db, "Txt Book", "txt book", "text", str(txt))
    import_root = tmp_path / "imp"
    integ = Integration(kind="storyteller", name="ST", base_url="http://h", api_key="pw",
                        config={"username": "u", "import_path": str(import_root)})
    db.add(integ)
    db.commit()
    db.refresh(integ)

    staged = {}

    class Fake:
        async def create_book(self, paths, collection=None):
            staged["paths"] = list(paths)
            return {"uuid": "bk1"}

        async def process(self, book_id):
            pass
    monkeypatch.setattr(companion, "client_for", lambda i: Fake())
    res = await companion._push_storyteller(db, integ)
    assert res["created"] == 1
    # The .txt was converted to an .epub before pushing.
    assert staged["paths"] and staged["paths"][0].endswith(".epub")
    assert os.path.isfile(staged["paths"][0])
