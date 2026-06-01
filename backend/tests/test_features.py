"""Integration tests for local-folder sync and the URL-index FTS search."""
from __future__ import annotations

import os
import tempfile

from sqlalchemy import select, text

from app import db as dbmod
from app.db import SessionLocal, index_fts_upsert, init_db
from app.ingestion.base import registry
from app.ingestion.engine import ensure_source
from app.ingestion.local_folder import sync_folder
from app.models import IndexedPage, IndexSite, WatchedFolder, Work


def _write(path: str, content: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


def test_local_folder_sync_add_update_remove():
    init_db()
    db = SessionLocal()
    folder_dir = tempfile.mkdtemp(prefix="shelf-folder-")
    book = os.path.join(folder_dir, "tale.txt")
    _write(book, "# One\nAlpha content.\n\n# Two\nBeta content.")

    folder = WatchedFolder(path=folder_dir, recursive=False, enabled=True)
    db.add(folder)
    db.commit()
    db.refresh(folder)

    src = ensure_source(db, registry.get("local_folder"))

    summary = sync_folder(db, folder)
    assert summary["added"] == 1
    work = db.scalar(
        select(Work).where(Work.source_id == src.id,
                           Work.source_work_ref == f"localfolder:{folder.id}:{book}")
    )
    assert work is not None and work.media_kind == "text"
    assert len(work.chapters) == 2

    # Unchanged -> no-op.
    assert sync_folder(db, folder) == {"added": 0, "updated": 0, "removed": 0, "errors": 0}

    # Change content + bump mtime -> update.
    _write(book, "# One\nAlpha.\n\n# Two\nBeta.\n\n# Three\nGamma.")
    os.utime(book, (os.stat(book).st_mtime + 5, os.stat(book).st_mtime + 5))
    summary = sync_folder(db, folder)
    assert summary["updated"] == 1
    db.refresh(work)
    assert len(work.chapters) == 3

    # Remove the file -> work deleted.
    os.remove(book)
    summary = sync_folder(db, folder)
    assert summary["removed"] == 1
    assert db.get(Work, work.id) is None
    db.close()


def test_index_fts_search_ranks_and_filters():
    init_db()
    if not dbmod.fts_enabled:
        return  # FTS5 not compiled in; LIKE fallback covered elsewhere
    db = SessionLocal()
    site = IndexSite(root_url="https://ex.com/", domain="ex.com", status="done",
                     max_pages=10, max_depth=2)
    db.add(site)
    db.commit()
    db.refresh(site)

    pages = [
        ("https://ex.com/a", "Dragons of the North", "A tale about ancient dragons and frost."),
        ("https://ex.com/b", "Gardening Tips", "How to grow tomatoes and basil in spring."),
        ("https://ex.com/c", "Dragon Cooking", "Recipes inspired by dragon legends and spice."),
    ]
    for url, title, body in pages:
        p = IndexedPage(site_id=site.id, url=url, title=title, text=body,
                        html=f"<p>{body}</p>", word_count=len(body.split()), status="fetched")
        db.add(p)
        db.flush()
        index_fts_upsert(db.connection(), p.id, title, body)
    db.commit()

    rows = db.execute(
        text(
            "SELECT p.title, bm25(indexed_pages_fts) FROM indexed_pages_fts f "
            "JOIN indexed_pages p ON p.id = f.rowid "
            'WHERE indexed_pages_fts MATCH \'"dragon"*\' ORDER BY bm25(indexed_pages_fts)'
        )
    ).all()
    titles = [r[0] for r in rows]
    assert "Dragons of the North" in titles and "Dragon Cooking" in titles
    assert "Gardening Tips" not in titles
    db.close()
