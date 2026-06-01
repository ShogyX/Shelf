"""Seed a fake Work + Chapters + Content so the frontend can be developed offline."""
from __future__ import annotations

import hashlib

from sqlalchemy import select

from .db import SessionLocal, init_db
from .ingestion.engine import sync_all_sources
from .models import Chapter, ChapterContent, Source, Work
from .sanitize import count_words, sanitize_html

_PARA = (
    "Rain fell in long silver threads over the tiled rooftops of the lower city, and Lin Yue "
    "pulled her hood close as she slipped between the shuttered stalls. The cultivation manual "
    "hidden against her chest seemed to grow heavier with every step. "
)


def run() -> None:
    init_db()
    db = SessionLocal()
    try:
        sync_all_sources(db)
        src = db.scalar(select(Source).where(Source.key == "memory"))
        existing = db.scalar(select(Work).where(Work.title.like("A Quiet Ascension%")))
        if existing:
            print(f"Seed already present (work id={existing.id}).")
            return
        work = Work(
            source_id=src.id if src else None,
            source_work_ref="seed-1",
            title="A Quiet Ascension (Seed)",
            author="Demo Author",
            description="A seeded demonstration serial for offline frontend development.",
            language="en",
            status="ongoing",
            hooked=False,
            total_chapters_known=8,
        )
        db.add(work)
        db.commit()
        db.refresh(work)

        for i in range(1, 9):
            html = sanitize_html(
                f"<h2>Chapter {i}</h2>" + "".join(f"<p>{_PARA * 4}</p>" for _ in range(5))
            )
            ch = Chapter(work_id=work.id, source_chapter_ref=f"seed-ch-{i}", index=i,
                         title=f"Chapter {i}: The Threshold", fetch_status="fetched")
            db.add(ch)
            db.flush()
            content = ChapterContent(
                chapter_id=ch.id, format="html", body=html,
                word_count=count_words(html),
                checksum=hashlib.sha256(html.encode()).hexdigest(),
            )
            db.add(content)
            db.flush()
            ch.content_id = content.id
        db.commit()
        print(f"Seeded work id={work.id} with 8 chapters.")
    finally:
        db.close()


if __name__ == "__main__":
    run()
