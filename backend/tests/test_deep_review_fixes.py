"""Unit checks for the deep-review backend fixes (downscale, hardcover media, comix author, aspect)."""
import io
from PIL import Image

from app.imagecache import downscale_image, COVER_MAX_EDGE, _too_wide_for_cover
from app.integrations.metadata import _hc_media_kind
from app.ingestion.adapters.comix import _comix_authors


def _jpeg(w, h):
    b = io.BytesIO()
    Image.new("RGB", (w, h), (120, 90, 60)).save(b, format="JPEG", quality=90)
    return b.getvalue()


def test_downscale_shrinks_oversized():
    big = _jpeg(2000, 3000)
    out, changed = downscale_image(big)
    assert changed
    w, h = Image.open(io.BytesIO(out)).size
    assert max(w, h) <= COVER_MAX_EDGE and len(out) < len(big)


def test_downscale_leaves_small_untouched():
    small = _jpeg(400, 600)
    out, changed = downscale_image(small)
    assert not changed and out == small


def test_downscale_non_image_noop():
    out, changed = downscale_image(b"not an image")
    assert not changed and out == b"not an image"


def test_hc_media_kind():
    assert _hc_media_kind({"genres": ["Manga", "Action"]}) == "comic"
    assert _hc_media_kind({"genres": ["Graphic Novels"]}) == "comic"
    assert _hc_media_kind({"genres": ["Fiction", "Romance"]}) == "text"
    assert _hc_media_kind({"genres": ["Light Novel"]}) == "text"  # prose, not comic
    assert _hc_media_kind({}) == "text"


def test_comix_authors():
    assert _comix_authors({"authors": [{"name": "Aka"}], "artists": [{"name": "Gotouge"}]}) == "Aka, Gotouge"
    assert _comix_authors({"authors": ["Solo"]}) == "Solo"
    assert _comix_authors({}) is None


def test_too_wide_for_cover():
    assert _too_wide_for_cover(_jpeg(575, 92)) is True     # banner
    assert _too_wide_for_cover(_jpeg(600, 900)) is False   # portrait cover
    assert _too_wide_for_cover(b"nope") is False           # undecodable → keep
