"""Catalog↔stock linking: mark each index entry with its on-disk in-stock Work by title."""
from __future__ import annotations

from sqlalchemy import delete, select

from app.db import SessionLocal, init_db
from app.ingestion.stock_link import link_catalog_to_stock
from app.models import CatalogGroup, CatalogWork, Source, Work


def _setup(db):
    for m in (CatalogWork, CatalogGroup, Work, Source):
        db.execute(delete(m))
    db.commit()
    src = Source(key="local_folder", display_name="lf", adapter_key="local_folder", tos_permitted=True)
    db.add(src); db.commit(); db.refresh(src)
    return src


def _work(db, src, title, path):
    w = Work(source_id=src.id, source_work_ref=f"localfolder:1:{path}", title=title,
             status="complete", local_path=path, local_size=100, media_kind="text")
    db.add(w); db.commit(); db.refresh(w)
    return w


def _group(db, title, hook=None):
    g = CatalogGroup(norm_key=title.lower(), title=title, media_bucket="text", hooked_work_id=hook)
    db.add(g); db.commit(); db.refresh(g)
    cw = CatalogWork(domain="d", work_url=f"u/{title}", title=title, norm_key=title.lower(),
                     media_kind="text", group_id=g.id, hooked_work_id=hook)
    db.add(cw); db.commit()
    return g


def test_links_and_fixes_catalog_to_stock():
    init_db(); db = SessionLocal()
    src = _setup(db)
    # an in-stock file titled by its full series/volume name
    warmage = _work(db, src, "The Spellmonger Series: Book 02 - Warmage", "/Books/Warmage/w.epub")
    other = _work(db, src, "A Monster Calls", "/Books/Stock/AMC/a.epub")
    # 1) un-hooked catalog entry whose volume name matches → linked
    g_new = _group(db, "Warmage: Spellmonger, Book 2", hook=None)
    # 2) catalog entry hooked to the WRONG work (restore corruption) → corrected
    g_wrong = _group(db, "Warmage", hook=other.id)

    out = link_catalog_to_stock(db)
    db.expire_all()
    assert db.get(CatalogGroup, g_new.id).hooked_work_id == warmage.id   # linked
    assert db.get(CatalogGroup, g_wrong.id).hooked_work_id == warmage.id # corrected from wrong work
    # the member catalog_work got the hook too
    assert db.scalar(select(CatalogWork.hooked_work_id).where(CatalogWork.group_id == g_new.id)) == warmage.id
    assert out["linked"] >= 1 and out["fixed"] >= 1


def _audio_work(db, src, title, path):
    w = Work(source_id=src.id, source_work_ref=f"audiobook:{title}", title=title,
             status="complete", local_path=path, local_size=100, media_kind="audio")
    db.add(w); db.commit(); db.refresh(w)
    return w


def test_never_hooks_audiobook_work():
    """An audiobook Work shares its ebook's title, but the catalog (an ebook/comic surface) must NEVER
    hook to it — the audiobook is a SEPARATE 'listen' format. Hooking it makes the ebook entry look
    acquired while the audio Work is hidden by the library view (the reported bug)."""
    init_db(); db = SessionLocal()
    src = _setup(db)
    _audio_work(db, src, "It", "/Audiobooks/It/it.m4b")   # only the audiobook is on disk
    g = _group(db, "It", hook=None)
    link_catalog_to_stock(db)
    db.expire_all()
    assert db.get(CatalogGroup, g.id).hooked_work_id is None             # NOT hooked to the audiobook
    assert db.scalar(select(CatalogWork.hooked_work_id).where(CatalogWork.group_id == g.id)) is None


def test_corrects_existing_audio_mishook():
    """A group already mis-hooked to an audiobook is self-healed: re-hooked to the ebook when one is on
    disk, else un-hooked (so it shows acquirable, not falsely in-stock/in-library)."""
    init_db(); db = SessionLocal()
    src = _setup(db)
    audio = _audio_work(db, src, "Hamlet", "/Audiobooks/Hamlet/h.m4b")
    ebook = _work(db, src, "Hamlet", "/Books/Hamlet/h.epub")
    g_fix = _group(db, "Hamlet", hook=audio.id)          # mis-hooked to audio, ebook on disk → re-hook
    audio2 = _audio_work(db, src, "Ulysses", "/Audiobooks/Ulysses/u.m4b")
    g_null = _group(db, "Ulysses", hook=audio2.id)       # mis-hooked, only audio on disk → un-hook
    link_catalog_to_stock(db)
    db.expire_all()
    assert db.get(CatalogGroup, g_fix.id).hooked_work_id == ebook.id     # corrected to the ebook
    assert db.get(CatalogGroup, g_null.id).hooked_work_id is None        # un-hooked (no ebook exists)
    assert db.scalar(select(CatalogWork.hooked_work_id).where(CatalogWork.group_id == g_fix.id)) == ebook.id
    assert db.scalar(select(CatalogWork.hooked_work_id).where(CatalogWork.group_id == g_null.id)) is None


def test_leaves_web_library_hooks_and_ambiguous_alone():
    init_db(); db = SessionLocal()
    src = _setup(db)
    # a crawled (file-less) Work — a deliberate web-library hook that must NOT be touched
    web = Work(source_id=src.id, source_work_ref="web:1", title="My Web Novel", status="ongoing",
               media_kind="text")
    db.add(web); db.commit(); db.refresh(web)
    g = _group(db, "My Web Novel", hook=web.id)
    out = link_catalog_to_stock(db)
    assert db.get(CatalogGroup, g.id).hooked_work_id == web.id   # untouched (no file behind it)
