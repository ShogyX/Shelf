"""Unit tests for the comix descramble repair (seam detection + body rewrite).

The browser-capture path itself needs a live reader and is exercised manually; here we cover the
cheap, deterministic pieces: the seam detector that decides *whether* a page is scrambled, and the
HTML-rewrite that swaps a scrambled figure's src for the captured one.
"""
from __future__ import annotations

import numpy as np
import pytest
from PIL import Image

from app.ingestion import descramble


def _smooth_page(w=300, h=400) -> Image.Image:
    """A smooth diagonal gradient — a well-ordered page has no grid-line discontinuities."""
    yy, xx = np.mgrid[0:h, 0:w]
    a = ((xx / w + yy / h) * 127).astype(np.uint8)
    return Image.fromarray(a, mode="L")


def _scramble_5x5(img: Image.Image) -> Image.Image:
    """Permute the 5×5 tiles of an image (the comix scramble shape) → strong seams in both axes."""
    a = np.asarray(img.convert("L")).copy()
    h, w = a.shape
    th, tw = h // 5, w // 5
    tiles = [a[r * th:(r + 1) * th, c * tw:(c + 1) * tw].copy()
             for r in range(5) for c in range(5)]
    perm = list(range(25))
    perm = perm[7:] + perm[:7]  # deterministic non-identity permutation
    out = np.zeros((th * 5, tw * 5), dtype=a.dtype)
    for idx, src in enumerate(perm):
        r, c = divmod(idx, 5)
        out[r * th:(r + 1) * th, c * tw:(c + 1) * tw] = tiles[src]
    return Image.fromarray(out, mode="L")


def test_is_long_strip_skips_webtoon_manhua(tmp_path, monkeypatch):
    """Manga pages (~1.4 aspect) descramble; webtoon/manhua long strips (tall) are skipped because
    their tall canvas never paints headless (the descrambler would butcher them into dark images)."""
    md = tmp_path / "media"
    (md / "imgcache").mkdir(parents=True)
    monkeypatch.setattr(descramble, "media_dir", lambda: md)

    def _page(name, w, h):
        Image.new("L", (w, h), 200).save(md / "imgcache" / name)
        return f"/media/imgcache/{name}"

    manga = [_page(f"mg{i}.png", 800, 1150) for i in range(6)]      # ~1.4 aspect
    manhua = [_page("banner.png", 900, 600)] + [_page(f"mh{i}.png", 900, 5000) for i in range(6)]
    assert descramble.is_long_strip(manga) is False
    assert descramble.is_long_strip(manhua) is True  # banner ignored, strips dominate


def test_capture_validity_rejects_empty():
    """A real captured page has high pixel variance; a failed/empty (dark, near-uniform) capture is
    rejected so the descrambler never replaces a real page with garbage."""
    import io

    def _png(arr):
        buf = io.BytesIO(); Image.fromarray(arr.astype("uint8"), "L").save(buf, "PNG"); return buf.getvalue()

    real = np.random.default_rng(0).integers(0, 255, (400, 300)).astype("uint8")  # high variance
    empty = np.full((400, 300), 35, dtype="uint8")  # near-uniform dark (the manhua garbage)
    assert descramble._capture_is_valid(_png(real)) is True
    assert descramble._capture_is_valid(_png(empty)) is False


def test_seam_detector_separates_scrambled_from_normal(tmp_path):
    normal_p = tmp_path / "normal.png"
    scram_p = tmp_path / "scrambled.png"
    page = _smooth_page()
    page.save(normal_p)
    _scramble_5x5(page).save(scram_p)

    s_normal = descramble._seam_min(normal_p)
    s_scram = descramble._seam_min(scram_p)
    assert s_normal is not None and s_scram is not None
    # The scrambled page must clear the threshold; the smooth one must stay well under it.
    assert s_scram >= descramble._SEAM_THRESHOLD
    assert s_normal < descramble._SEAM_THRESHOLD


def test_seam_min_handles_unreadable_path(tmp_path):
    assert descramble._seam_min(tmp_path / "nope.png") is None


def test_chapter_page_srcs_in_order():
    body = (
        '<div class="comic">'
        '<figure class="comic-page"><img alt="" src="/media/imgcache/a.webp"/></figure>'
        '<figure class="comic-page"><img alt="" src="/media/imgcache/b.webp"/></figure>'
        "</div>"
    )
    assert descramble.chapter_page_srcs(body) == ["/media/imgcache/a.webp", "/media/imgcache/b.webp"]


def test_img_src_rewrite_swaps_only_targeted():
    body = (
        '<figure class="comic-page"><img alt="" src="/media/imgcache/a.webp"/></figure>'
        '<figure class="comic-page"><img alt="" src="/media/imgcache/b.webp"/></figure>'
    )
    replaced = {"/media/imgcache/b.webp": "/media/descrambled/1/2/0002.png"}

    def _swap(m):
        new = replaced.get(m.group(2))
        return f"{m.group(1)}{new}{m.group(3)}" if new else m.group(0)

    out = descramble._IMG_SRC.sub(_swap, body)
    assert "/media/imgcache/a.webp" in out  # untouched
    assert "/media/descrambled/1/2/0002.png" in out  # swapped
    assert "/media/imgcache/b.webp" not in out
