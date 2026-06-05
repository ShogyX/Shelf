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
