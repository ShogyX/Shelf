"""Language-aware library dedup: an English AND a Norwegian edition of a title coexist, but a second
same-language same-format copy is pruned. Plus the download-tracker edition_exists guard."""
from __future__ import annotations

from sqlalchemy import delete, select

import app.ingestion.adapters  # noqa: F401 — register the local_folder adapter
from app.db import SessionLocal, init_db
from app.ingestion import dedup
from app.ingestion import downloads as dl
from app.models import Work


def _w(db, src, title, author, mk, lang, path, chash=None):
    w = Work(source_id=src.id, source_work_ref=f"t:{path}", title=title, author=author,
             media_kind=mk, language=lang, local_path=path, content_hash=chash, status="complete")
    db.add(w); db.commit(); db.refresh(w)
    return w


def _reset(db):
    db.execute(delete(Work)); db.commit()
    return dl._local_source(db)


def test_dedup_keeps_english_and_norwegian_prunes_duplicate(tmp_path):
    init_db(); db = SessionLocal(); src = _reset(db)
    _w(db, src, "Realm Breaker", "Victoria Aveyard", "text", "en", str(tmp_path / "en1.epub"))
    _w(db, src, "Realm Breaker", "Victoria Aveyard", "text", "en", str(tmp_path / "en2.epub"))  # dup EN
    _w(db, src, "Realm Breaker", "Victoria Aveyard", "text", "no", str(tmp_path / "no1.epub"))   # NO — keep
    _w(db, src, "Realm Breaker", "Victoria Aveyard", "audio", "en", str(tmp_path / "en.mp3"))    # audiobook — keep
    stats = dedup.run(db, execute=True)
    assert stats["pruned"] == 1                       # only the 2nd English EBOOK
    rows = db.scalars(select(Work).where(Work.title == "Realm Breaker")).all()
    # one EN ebook + one NO ebook + one EN audiobook survive (3), the duplicate EN ebook is gone
    survivors = sorted((w.media_kind, w.language) for w in rows)
    assert survivors == [("audio", "en"), ("text", "en"), ("text", "no")]
    db.close()


def test_dedup_collapses_byte_identical_regardless_of_title(tmp_path):
    init_db(); db = SessionLocal(); src = _reset(db)
    _w(db, src, "Clean Title", "Author X", "text", "en", str(tmp_path / "a.epub"), chash="H")
    _w(db, src, "mangled.title.retail.epub", "Author X", "text", "en", str(tmp_path / "b.epub"), chash="H")
    stats = dedup.run(db, execute=True)
    assert stats["pruned"] == 1
    db.close()


def test_edition_exists_is_language_and_format_aware(tmp_path):
    init_db(); db = SessionLocal(); src = _reset(db)
    _w(db, src, "Wool", "Hugh Howey", "text", "en", str(tmp_path / "w.epub"))
    assert dedup.edition_exists(db, title="Wool", author="Hugh Howey", media_kind="text", lang="en")
    assert dedup.edition_exists(db, title="Wool", author=None, media_kind="text", lang="en")   # unknown author OK
    assert not dedup.edition_exists(db, title="Wool", author="Hugh Howey", media_kind="text", lang="no")   # NO not held
    assert not dedup.edition_exists(db, title="Wool", author="Hugh Howey", media_kind="audio", lang="en")  # audio not held
    assert not dedup.edition_exists(db, title="Wool", author="Someone Else", media_kind="text", lang="en")  # diff author
    db.close()


def test_dedup_merges_author_spelling_variants(tmp_path):
    """P7: same title/media/language with author SPELLING variants (J.K. Rowling / Joanne K. Rowling /
    J K Rowling) collapse to one edition, but a genuinely different author of a same-titled book stays
    separate — token overlap, not exact-string match."""
    init_db(); db = SessionLocal(); src = _reset(db)
    _w(db, src, "Half-Blood Prince", "J.K. Rowling", "audio", "en", str(tmp_path / "a.m4b"))
    _w(db, src, "Half-Blood Prince", "Joanne K. Rowling", "audio", "en", str(tmp_path / "b.m4b"))
    _w(db, src, "Half-Blood Prince", "J K Rowling", "audio", "en", str(tmp_path / "c.mp3"))
    _w(db, src, "Half-Blood Prince", "Unrelated Ghostwriter", "audio", "en", str(tmp_path / "d.mp3"))
    stats = dedup.run(db, execute=True)
    assert stats["pruned"] == 2                        # 3 Rowling variants → 1 keeper
    rows = db.scalars(select(Work).where(Work.title == "Half-Blood Prince")).all()
    assert len(rows) == 2                              # one Rowling + the distinct other author
    assert any("Ghostwriter" in (w.author or "") for w in rows)
    db.close()
