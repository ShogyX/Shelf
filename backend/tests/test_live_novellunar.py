"""Opt-in LIVE check: the smart classifier/catalog recognize the real novellunar page.

Run with:  SHELF_LIVE=1 pytest tests/test_live_novellunar.py -s
Skipped by default so normal CI stays offline + fast.
"""
from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("SHELF_LIVE") != "1" and not os.path.exists("/tmp/shelf_live"),
    reason="set SHELF_LIVE=1 (or touch /tmp/shelf_live) to hit the network",
)

LOHP = "https://novellunar.com/novel/library-of-heavens-path-v1"
CH1 = "https://novellunar.com/novel/library-of-heavens-path-v1/chapter/1"


def _get(url: str) -> str:
    import httpx

    r = httpx.get(url, follow_redirects=True, timeout=30,
                  headers={"User-Agent": "ShelfBot/1.0 (+catalog-test)"})
    r.raise_for_status()
    return r.text


def test_live_classify_and_catalog_novellunar():
    from app.ingestion.extract import classify_page

    html = _get(LOHP)
    pc = classify_page(html, LOHP)
    print(f"\nnovel page → kind={pc.kind} advertised={pc.advertised} "
          f"listed={pc.listed} title={pc.title!r}")
    assert pc.kind in ("work", "toc")
    assert pc.work_url == LOHP

    ch_html = _get(CH1)
    cpc = classify_page(ch_html, CH1)
    print(f"chapter page → kind={cpc.kind} work_url={cpc.work_url}")
    assert cpc.kind == "chapter"
    assert cpc.work_url == LOHP


def _adapter(key: str):
    from app.db import SessionLocal, init_db
    from app.ingestion.base import registry
    from app.ingestion.engine import ensure_source, get_fetcher

    init_db()
    db = SessionLocal()
    ensure_source(db, registry.get(key))  # configures the fetcher budget for this source
    db.close()
    return registry.get(key)(get_fetcher())


async def _verify_book(adapter, ref: str, *, min_chapters: int, expect_in_title: str):
    """Pull a whole-book source and assert the content is real + boilerplate-free."""
    from bs4 import BeautifulSoup as BS

    meta = await adapter.discover_work(ref)
    print(f"\n{adapter.key} → title={meta.title!r} author={meta.author!r}")
    assert expect_in_title.lower() in (meta.title or "").lower()
    refs = await adapter.list_chapters(meta)
    print(f"  chapters listed: {len(refs)}")
    assert len(refs) >= min_chapters, f"only {len(refs)} chapters"
    raw = await adapter.fetch_chapter(refs[0])
    text = BS(raw.body, "lxml").get_text(" ", strip=True)
    print(f"  ch1 {len(text)} chars: {text[:80]!r}")
    assert len(text) > 200, "chapter body is empty/too short"
    assert "Project Gutenberg" not in text  # license boilerplate must be stripped


@pytest.mark.asyncio
async def test_live_gutenberg_tom_sawyer():
    # #74 = The Adventures of Tom Sawyer (~35 chapters). Reproduces the bug report.
    await _verify_book(_adapter("gutenberg"), "74",
                       min_chapters=30, expect_in_title="Tom Sawyer")


@pytest.mark.asyncio
async def test_live_gutenberg_huck_finn():
    # #76 = Adventures of Huckleberry Finn (~43 chapters).
    await _verify_book(_adapter("gutenberg"), "76",
                       min_chapters=30, expect_in_title="Huckleberry Finn")


@pytest.mark.asyncio
async def test_live_standardebooks():
    await _verify_book(_adapter("standardebooks"),
                       "mark-twain/the-adventures-of-tom-sawyer",
                       min_chapters=20, expect_in_title="Tom Sawyer")
