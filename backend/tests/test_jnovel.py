"""J-Novel Club adapter — v2 series → volumes → parts enumeration + part fetch (mocked API).

The real labs API is Cloudflare-fronted (fetched via the headless browser, returning a
RenderedPage whose .body_text is the JSON); these tests mock that shape.
"""
from __future__ import annotations

import json

import pytest

from app.ingestion.adapters.jnovel import JNovelClubAdapter, _series_slug

SLUG = "reborn-to-reign"


def test_series_slug_from_various_refs():
    assert _series_slug(f"https://j-novel.club/series/{SLUG}") == SLUG
    assert _series_slug(
        "https://j-novel.club/read/reborn-to-reign-volume-1-part-3"
    ) == "reborn-to-reign"
    assert _series_slug("reborn-to-reign") == SLUG


class _Rendered:
    """Mimics browser.RenderedPage: .body_text holds JSON, .text the HTML, plus status."""
    def __init__(self, *, body=None, text="", status=200):
        self.body_text = json.dumps(body) if body is not None else text
        self.text = text or (json.dumps(body) if body is not None else "")
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeFetcher:
    async def get_html(self, source_key, url, *, headers=None, force_render=False, **kw):
        assert force_render, "j-novel must force the headless browser (Cloudflare)"
        if url.endswith(f"/series/{SLUG}?format=json"):
            return _Rendered(body={"id": "SER-X", "type": "NOVEL", "title": "Reborn to Reign",
                                   "description": "A nameless man reborn.",
                                   "cover": {"coverUrl": "https://cdn/cover.jpg"}})
        if url.endswith("/series/some-manga?format=json"):
            return _Rendered(body={"id": "SER-M", "type": "MANGA", "title": "Some Manga"})
        if url.endswith(f"/series/{SLUG}/volumes?format=json"):
            return _Rendered(body={"volumes": [{"id": "VOL-1"}, {"id": "VOL-2"}]})
        if "/volumes/VOL-1/parts" in url:
            return _Rendered(body={"parts": [{"id": "PRT-1", "title": "Volume 1 Part 1"},
                                             {"id": "PRT-2", "title": "Volume 1 Part 2"}]})
        if "/volumes/VOL-2/parts" in url:
            return _Rendered(body={"parts": [{"id": "PRT-3", "title": "Volume 2 Part 1"}]})
        if "/parts/PRT-1/data.xhtml" in url:
            return _Rendered(text='<p>Chapter text.</p><img src="/img/fig.jpg"/>', status=200)
        if "/parts/PRT-LOCKED/data.xhtml" in url:
            return _Rendered(text="BLITZ 1.2.12", status=418)
        raise AssertionError(f"unexpected {url}")


@pytest.fixture
def adapter():
    return JNovelClubAdapter(_FakeFetcher())


async def test_discover_work(adapter):
    meta = await adapter.discover_work(f"https://j-novel.club/series/{SLUG}")
    assert meta.title == "Reborn to Reign"
    assert meta.media_kind == "text"
    assert meta.cover_url == "https://cdn/cover.jpg"
    assert meta.source_work_ref == SLUG


async def test_manga_series_is_comic(adapter):
    meta = await adapter.discover_work("https://j-novel.club/series/some-manga")
    assert meta.media_kind == "comic"


async def test_list_chapters_enumerates_all_parts(adapter):
    meta = await adapter.discover_work(f"https://j-novel.club/series/{SLUG}")
    chs = await adapter.list_chapters(meta)
    assert [c.source_chapter_ref for c in chs] == ["PRT-1", "PRT-2", "PRT-3"]
    assert [c.index for c in chs] == [1, 2, 3]
    assert chs[0].title == "Volume 1 Part 1"


async def test_fetch_part_resolves_relative_images(adapter):
    from app.ingestion.base import ChapterRef
    raw = await adapter.fetch_chapter(ChapterRef(source_chapter_ref="PRT-1", index=1, title="P1"))
    assert "Chapter text." in raw.body
    assert "https://labs.j-novel.club/img/fig.jpg" in raw.body


async def test_members_only_part_raises(adapter):
    from app.ingestion.base import ChapterRef, PermanentFetchError
    # Permanent (not transient) so the scheduler marks it 'unavailable' and never retries.
    with pytest.raises(PermanentFetchError, match="members-only"):
        await adapter.fetch_chapter(
            ChapterRef(source_chapter_ref="PRT-LOCKED", index=1, title="P9")
        )
