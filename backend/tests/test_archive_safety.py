"""Batch-2 file/archive safety (review 2026-06-15): epub_export path containment (F11), comic
archive zip-bomb caps (F27), upload size cap (F19)."""
from __future__ import annotations

import io
import zipfile

import pytest


# ------------------------------------------------------------------ F11: export path containment
def test_local_image_bytes_blocks_media_traversal(monkeypatch, tmp_path):
    from app import epub_export as E
    media = tmp_path / "media"
    media.mkdir()
    (media / "ok.jpg").write_bytes(b"\xff\xd8\xffOK")
    monkeypatch.setattr(E, "media_dir", lambda: media)
    # A legitimate in-root image resolves.
    res = E._local_image_bytes("/media/ok.jpg")
    assert res is not None and res[0] == b"\xff\xd8\xffOK"
    # A traversal escape is refused (would otherwise read an arbitrary server file).
    (tmp_path / "secret").write_bytes(b"TOPSECRET")
    assert E._local_image_bytes("/media/../secret") is None
    assert E._local_image_bytes("/media/../../../../etc/hostname") is None


# ------------------------------------------------------------------ F27: comic zip-bomb caps
def _cbz(n_pages: int) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for i in range(n_pages):
            zf.writestr(f"{i:04d}.jpg", b"\xff\xd8\xff\xe0jpeg")
    return buf.getvalue()


def test_comic_images_reads_normal_archive():
    from app.ingestion import media as MD
    entries = MD._comic_images(_cbz(3), ".cbz")
    assert len(entries) == 3


def test_comic_images_rejects_too_many_pages(monkeypatch):
    from app.ingestion import media as MD
    monkeypatch.setattr(MD, "_MAX_COMIC_PAGES", 2)
    with pytest.raises(RuntimeError):
        MD._comic_images(_cbz(3), ".cbz")


def test_comic_images_rejects_oversized_decompressed(monkeypatch):
    from app.ingestion import media as MD
    monkeypatch.setattr(MD, "_MAX_COMIC_DECOMPRESSED", 4)  # 4 bytes; any real page exceeds it
    with pytest.raises(RuntimeError):
        MD._comic_images(_cbz(2), ".cbz")


# ------------------------------------------------------------------ F19: upload size cap helper
class _FakeUpload:
    def __init__(self, data: bytes):
        self._buf = io.BytesIO(data)

    async def read(self, n: int = -1) -> bytes:
        return self._buf.read(n)


@pytest.mark.asyncio
async def test_read_capped_under_limit():
    from app.routers import jobs
    out = await jobs._read_capped(_FakeUpload(b"x" * 100), 1000)
    assert out == b"x" * 100


@pytest.mark.asyncio
async def test_read_capped_over_limit_returns_none():
    from app.routers import jobs
    out = await jobs._read_capped(_FakeUpload(b"x" * 2000), 1000)
    assert out is None
