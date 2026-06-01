"""Index hooking (page + whole-site) and EPUB image embedding (comics)."""
from __future__ import annotations

import io
import zipfile

from sqlalchemy import select

from app.db import SessionLocal, init_db
from app.epub_export import EpubChapter, build_epub
from app.ingestion.base import registry
from app.ingestion.engine import ensure_source
from app.media import comic_dir, comic_url
from app.models import IndexedPage, IndexSite
from app.routers.index import hook_page, hook_site


def _make_site_with_pages(db, n=3):
    site = IndexSite(root_url="https://ex.com/", domain="ex.com", status="done",
                     max_pages=10, max_depth=2, title="Example Wiki")
    db.add(site)
    db.commit()
    db.refresh(site)
    for i in range(1, n + 1):
        db.add(IndexedPage(
            site_id=site.id, url=f"https://ex.com/p{i}", title=f"Page {i}",
            description=f"About page {i}", author="Ex Author", cover_url="https://ex.com/c.jpg",
            html=f"<p>Body of page {i}.</p>", text=f"Body of page {i}.",
            word_count=4, status="fetched",
        ))
    db.commit()
    return site


def test_hook_single_page_creates_work():
    init_db()
    db = SessionLocal()
    site = _make_site_with_pages(db, 1)
    page = db.scalar(select(IndexedPage).where(IndexedPage.site_id == site.id))

    work = hook_page(page.id, db)
    assert work.id and work.title == "Page 1"
    assert work.author == "Ex Author" and work.cover_url == "https://ex.com/c.jpg"
    assert len(work.chapters) == 1
    db.refresh(page)
    assert page.hooked_work_id == work.id
    db.close()


def test_hook_whole_site_creates_multichapter_work():
    init_db()
    db = SessionLocal()
    site = _make_site_with_pages(db, 3)

    work = hook_site(site.id, db)
    assert work.title == "Example Wiki"
    assert work.total_chapters_known == 3
    assert len(work.chapters) == 3
    # Every page now points at the work.
    pages = db.scalars(select(IndexedPage).where(IndexedPage.site_id == site.id)).all()
    assert all(p.hooked_work_id == work.id for p in pages)
    db.close()


def test_epub_embeds_local_comic_images():
    init_db()
    # Write two fake page images into the media dir the way comic import does.
    key = "epubtest"
    d = comic_dir(key)
    (d / "0001.png").write_bytes(b"\x89PNG-fake-1")
    (d / "0002.png").write_bytes(b"\x89PNG-fake-2")
    body = (
        f'<div class="comic">'
        f'<figure class="comic-page"><img src="{comic_url(key, "0001.png")}"/></figure>'
        f'<figure class="comic-page"><img src="{comic_url(key, "0002.png")}"/></figure>'
        f"</div>"
    )
    data = build_epub(
        title="Test Comic", author=None, language="en", cover_url=None,
        chapters=[EpubChapter(index=1, title="Issue 1", body_html=body)],
        identifier="urn:test:comic",
    )
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = zf.namelist()
        imgs = [n for n in names if n.lower().endswith(".png")]
        assert len(imgs) == 2, names
        chap = next(n for n in names if n.endswith(".xhtml") and "chap_" in n)
        xhtml = zf.read(chap).decode("utf-8")
        # src rewritten to the in-EPUB image path, not the original /media URL.
        assert "images/img_" in xhtml and "/media/" not in xhtml


def test_pdf_parse_routes_and_titles_from_filename():
    # Minimal valid PDF via pypdf (blank page -> no text -> single chapter).
    from pypdf import PdfWriter

    from app.ingestion.media import parse_media

    w = PdfWriter()
    w.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    w.write(buf)
    parsed = parse_media(buf.getvalue(), "My Report.pdf")
    assert parsed.kind == "text"
    assert parsed.title == "My Report"
    assert len(parsed.chapters) >= 1


def test_web_index_adapter_registered():
    init_db()
    db = SessionLocal()
    src = ensure_source(db, registry.get("web_index"))
    assert src.key == "web_index" and src.robots_respected is True
    db.close()
