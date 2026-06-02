"""MangaDex adapter — metadata, chapter feed, and at-home image assembly (mocked API)."""
from __future__ import annotations

import pytest

from app.ingestion.adapters.mangadex import MangaDexAdapter

MID = "0a580438-bc72-4503-940b-12a5da881b56"


class _Resp:
    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._p


class _FakeFetcher:
    """Serves canned MangaDex API JSON based on the request path."""

    async def get(self, source_key, url, **kw):
        if f"/manga/{MID}?" in url or url.endswith(f"/manga/{MID}"):
            return _Resp({"data": {
                "id": MID,
                "attributes": {"title": {"en": "You Are a Four-Leaf Clover"},
                               "description": {"en": "A gentle romance."},
                               "status": "ongoing", "originalLanguage": "ja"},
                "relationships": [
                    {"type": "cover_art", "attributes": {"fileName": "abc.jpg"}},
                    {"type": "author", "attributes": {"name": "Koushi"}},
                ],
            }})
        if "/feed" in url:
            offset = int(url.split("offset=")[1].split("&")[0])
            if offset > 0:
                return _Resp({"data": [], "total": 2})
            return _Resp({"data": [
                {"id": "ch1", "attributes": {"chapter": "1", "pages": 52}},
                {"id": "ch1-dup", "attributes": {"chapter": "1", "pages": 52}},  # other group
                {"id": "ext", "attributes": {"chapter": "2", "pages": 0,
                                             "externalUrl": "https://x"}},  # skipped
                {"id": "ch3", "attributes": {"chapter": "3", "pages": 10}},
            ], "total": 2})
        if "/at-home/server/ch1" in url:
            return _Resp({"baseUrl": "https://cdn.mangadex.network",
                          "chapter": {"hash": "HASH", "data": ["p1.png", "p2.png", "p3.png"]}})
        if "/at-home/server/chDS" in url:  # only data-saver pages available
            return _Resp({"baseUrl": "https://cdn.mangadex.network",
                          "chapter": {"hash": "H2", "data": [], "dataSaver": ["s1.jpg", "s2.jpg"]}})
        raise AssertionError(f"unexpected url {url}")


@pytest.fixture
def adapter():
    return MangaDexAdapter(_FakeFetcher())


def test_manga_id_from_url():
    assert MangaDexAdapter._manga_id(f"https://mangadex.org/title/{MID}/slug") == MID
    assert MangaDexAdapter._manga_id(MID) == MID


async def test_fetch_chapter_falls_back_to_data_saver(adapter):
    raw = await adapter.fetch_chapter(
        type("R", (), {"source_chapter_ref": "chDS", "title": "Chapter 2"})()
    )
    assert raw.body.count('class="comic-page"') == 2
    assert "https://cdn.mangadex.network/data-saver/H2/s1.jpg" in raw.body


async def test_discover_work(adapter):
    meta = await adapter.discover_work(f"https://mangadex.org/title/{MID}/kimi")
    assert meta.title == "You Are a Four-Leaf Clover"
    assert meta.author == "Koushi"
    assert meta.cover_url.endswith("/covers/" + MID + "/abc.jpg.512.jpg")
    # MangaDex is always image-based manga → the work must be tagged a comic, so the reader
    # and library treat it as images rather than a prose book.
    assert meta.media_kind == "comic"


async def test_list_chapters_dedupes_and_skips_empty(adapter):
    meta = await adapter.discover_work(f"https://mangadex.org/title/{MID}/kimi")
    chs = await adapter.list_chapters(meta)
    # Duplicate ch1 collapsed, the page-less external ch2 skipped → ch1 + ch3.
    assert [c.title for c in chs] == ["Chapter 1", "Chapter 3"]
    assert chs[0].source_chapter_ref == "ch1"


async def test_fetch_chapter_builds_comic_image_strip(adapter):
    raw = await adapter.fetch_chapter(
        type("R", (), {"source_chapter_ref": "ch1", "title": "Chapter 1"})()
    )
    assert raw.body.count("<figure class=\"comic-page\">") == 3
    assert "https://cdn.mangadex.network/data/HASH/p1.png" in raw.body
    assert 'class="comic"' in raw.body


def test_disabled_by_default_and_robots_unrespected():
    # Robots-disallowed image path → ships off until an operator attests/enables it.
    assert MangaDexAdapter.compliance.tos_permitted_default is False
    assert MangaDexAdapter.compliance.robots_respected is False
