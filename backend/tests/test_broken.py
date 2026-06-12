"""Broken-release registry + multi-strategy search (broken links filtered, queries merged)."""
from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from sqlalchemy import delete

from app.db import SessionLocal, init_db
from app.ingestion import broken
from app.ingestion import release_matcher as rm
from app.models import BrokenRelease, CatalogWork, Integration


@dataclass
class Rel:
    title: str
    download_url: str | None = "http://idx/nzb"
    guid: str | None = None
    indexer: str = "NzbPlanet"
    size: int = 10_000_000
    categories: list = field(default_factory=lambda: [7000, 7020])
    grabs: int = 0


def _fresh():
    init_db()
    db = SessionLocal()
    db.execute(delete(BrokenRelease)); db.execute(delete(CatalogWork)); db.execute(delete(Integration))
    db.commit()
    return db


def test_mark_and_check_broken_idempotent():
    db = _fresh()
    r = Rel("Andy.Weir-Project.Hail.Mary.EPUB", guid="g42")
    assert not broken.is_broken(db, r)
    broken.mark_broken(db, r, reason="corrupt")
    broken.mark_broken(db, r, reason="corrupt")   # idempotent — no duplicate / no raise
    assert broken.is_broken(db, r)
    assert broken.broken_keys(db) == {"guid:g42"}
    db.close()


@pytest.mark.asyncio
async def test_find_releases_merges_variants_and_drops_broken(monkeypatch):
    db = _fresh()
    db.add(Integration(kind="prowlarr", name="P", base_url="http://p", api_key="k", enabled=True,
                       config={"categories": [7000, 7020], "preferred_formats": ["epub"]}))
    cw = CatalogWork(provider="openlibrary", provider_ref="/works/PHM", domain="openlibrary.org",
                     work_url="x", title="Project Hail Mary", author="Andy Weir",
                     media_kind="text", norm_key="project hail mary")
    db.add(cw); db.commit(); db.refresh(cw)

    # Pre-mark one release broken so it must never appear in the ranked candidates.
    broken.mark_broken(db, Rel("Andy.Weir-Project.Hail.Mary.EPUB", guid="dead"), reason="missing blocks")

    seen_queries: list[tuple[str, str]] = []

    async def fake_search(self, query, *, categories=None, indexer_ids=None,
                          protocols=("usenet",), limit=100, offset=0, search_type="search"):
        seen_queries.append((query, search_type))
        # Each variant returns the dead release + one good release unique to that query.
        return [
            Rel("Andy.Weir-Project.Hail.Mary.EPUB", guid="dead"),
            Rel(f"Andy.Weir-Project.Hail.Mary.Retail.EPUB", guid=f"ok-{len(seen_queries)}"),
        ]

    from app.integrations.prowlarr import ProwlarrClient
    monkeypatch.setattr(ProwlarrClient, "search", fake_search)

    ranked = await rm.find_releases(db, cw)
    # Multiple distinct free-text query variants were issued (multi-strategy search) …
    text_qs = [q for q, t in seen_queries if t == "search"]
    assert len(text_qs) >= 3 and len(set(text_qs)) == len(text_qs)
    # … plus a structured book-search pass (13A).
    assert any(t == "book" for _, t in seen_queries)
    keys = {broken.release_key(s.release) for s in ranked}
    assert "guid:dead" not in keys           # broken link filtered out
    assert any(k and k.startswith("guid:ok-") for k in keys)
    db.close()
