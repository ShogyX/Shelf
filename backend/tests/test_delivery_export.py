"""P7: comic→Kindle export bounds memory + fails the email cap BEFORE building the whole EPUB."""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.routers import delivery


class _Ch:
    index = 1
    body_html = '<img src="/media/a.jpg"/><img src="/media/b.jpg"/>'


class _Work:
    id = 1
    title = "Big Comic"
    author = None
    language = "en"


def test_gather_kindle_comic_caps_raw_bytes_before_building(monkeypatch):
    # Tiny ceiling so two small pages trip it; raw bytes accumulate past it on the 2nd page.
    monkeypatch.setattr(delivery, "_KINDLE_RAW_CAP_BYTES", 10)
    monkeypatch.setattr(delivery, "_gather", lambda db, work, start, limit: [_Ch()])
    monkeypatch.setattr(delivery, "resolve_image_bytes",
                        lambda src, cache: (b"X" * 8, "jpg", "image/jpeg"))
    built = {"n": 0}

    def _build(**kw):
        built["n"] += 1
        return b"epub", 1
    monkeypatch.setattr(delivery, "build_kindle_comic_epub", _build)

    with pytest.raises(HTTPException) as ei:
        delivery.gather_kindle_comic(None, _Work(), 1, None)
    assert ei.value.status_code == 413
    assert built["n"] == 0   # bailed on accumulating raw bytes, NOT after building the EPUB


def test_gather_kindle_comic_builds_when_under_cap(monkeypatch):
    monkeypatch.setattr(delivery, "_KINDLE_RAW_CAP_BYTES", 10_000)  # ample
    monkeypatch.setattr(delivery, "_gather", lambda db, work, start, limit: [_Ch()])
    monkeypatch.setattr(delivery, "resolve_image_bytes",
                        lambda src, cache: (b"X" * 8, "jpg", "image/jpeg"))
    monkeypatch.setattr(delivery, "build_kindle_comic_epub", lambda **kw: (b"epub-bytes", 2))

    out = delivery.gather_kindle_comic(None, _Work(), 1, None)
    assert out is not None
    epub_bytes, filename, pages = out
    assert epub_bytes == b"epub-bytes" and pages == 2 and filename.endswith(".epub")
