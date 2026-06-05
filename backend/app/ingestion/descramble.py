"""Descramble already-captured comic pages (comix.to).

comix.to serves a subset of each chapter's page images *pre-scrambled* (a keyed tile permutation,
consistently at fixed early positions — pages 4 & 8 on the series seen so far) and reassembles them
onto a <canvas> in the reader; normal pages are plain <img>. Our CDN-enumeration fetch path can't
tell the two apart, so a scrambled page is stored as the raw (scrambled) image. This module repairs
them by **browser capture**: render the reader and screenshot the pages it draws onto a <canvas>
(the site's own WASM does the unscramble), then rewrite those figures in the stored chapter HTML to
point at the captured images.

Detection is the render itself (canvas == scrambled) — that is ground truth. Seam analysis on the
stored bytes (``looks_scrambled``) is kept only as a diagnostic: it works for some series but MISSES
others entirely (One Piece's scramble leaves no 1/5-grid seams), so it must NOT gate which chapters
render. Every captured comix chapter is rendered once (then marked ``Chapter.descrambled_at``).

Gated to operator-permitted sources via the same fetcher/compliance path as normal rendering.
"""
from __future__ import annotations

import logging
import re

from sqlalchemy.orm import Session

from ..media import descramble_dir, descramble_url, media_dir
from ..models import Chapter, Work

log = logging.getLogger("shelf.descramble")

# A page is flagged scrambled when the seam energy at the interior 5×5 grid lines, relative to the
# image's average gradient, is elevated in BOTH axes. Validated on live comix pages: scrambled
# pages scored min(v,h) ≈ 2.8–6.5 while every normal page scored ≤ 1.2 (a single panel border
# elevates only one axis). 1.6 sits in the clear gap with margin on both sides.
_SEAM_THRESHOLD = 1.6
_GRID = 5  # comix uses a 5×5 tile permutation

_IMG_SRC = re.compile(r'(<img\b[^>]*\bsrc=")(/media/[^"]+)(")', re.I)

# Captured canvases come back as large 2× PNGs (≈3–4 MB). Stored as-is they decode slowly in the
# reader, so flipping to a descrambled page shows a brief wrong-size frame ('zoom' glitch). Encode
# them like the source pages instead: downscale to ~source width and save lossy WebP (~300 KB,
# decodes instantly — on par with the normal CDN pages around them).
_CAPTURE_MAX_W = 1400
_CAPTURE_QUALITY = 88


def _encode_capture(png: bytes) -> tuple[str, bytes]:
    """(extension, bytes) for a captured page — downscaled WebP, falling back to the raw PNG."""
    try:
        import io

        from PIL import Image

        with Image.open(io.BytesIO(png)) as im:
            im = im.convert("RGB")
            if im.width > _CAPTURE_MAX_W:
                h = round(im.height * _CAPTURE_MAX_W / im.width)
                im = im.resize((_CAPTURE_MAX_W, h), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, format="WEBP", quality=_CAPTURE_QUALITY, method=6)
            return "webp", buf.getvalue()
    except Exception:
        return "png", png


def _local_path(media_url: str):
    """Map a served '/media/...' URL back to its file on disk (None if not local)."""
    if not media_url.startswith("/media/"):
        return None
    return media_dir() / media_url.split("/media/", 1)[1]


def _seam_min(path) -> float | None:
    """min(vertical, horizontal) normalized seam energy at the interior grid lines.

    Returns None if the image can't be read. Higher → more likely scrambled."""
    try:
        import numpy as np
        from PIL import Image

        with Image.open(path) as im:
            a = np.asarray(im.convert("L"), dtype=np.float32)
    except Exception:
        return None
    if a.ndim != 2 or min(a.shape) < _GRID * 4:
        return None
    h, w = a.shape

    dcol = np.abs(np.diff(a, axis=1))           # discontinuity between adjacent columns
    base_col = float(dcol.mean()) + 1e-6
    vx = [round(w * k / _GRID) for k in range(1, _GRID)]
    vseam = float(np.mean([dcol[:, x - 1].mean() for x in vx])) / base_col

    drow = np.abs(np.diff(a, axis=0))
    base_row = float(drow.mean()) + 1e-6
    hy = [round(h * k / _GRID) for k in range(1, _GRID)]
    hseam = float(np.mean([drow[y - 1, :].mean() for y in hy])) / base_row

    return min(vseam, hseam)


def chapter_page_srcs(body: str) -> list[str]:
    """The ordered '/media/..' page-image URLs of a stored comic chapter body."""
    return [m.group(2) for m in _IMG_SRC.finditer(body or "")]


def looks_scrambled(body: str) -> list[int]:
    """1-based indices of page images in a stored chapter body that look scrambled (cheap)."""
    flagged: list[int] = []
    for i, src in enumerate(chapter_page_srcs(body), start=1):
        p = _local_path(src)
        if p is None:
            continue
        score = _seam_min(p)
        if score is not None and score >= _SEAM_THRESHOLD:
            flagged.append(i)
    return flagged


def _reader_url(chapter: Chapter) -> str | None:
    ref = (chapter.source_chapter_ref or "").strip()
    if not ref:
        return None
    if ref.startswith("http"):
        return ref
    return "https://comix.to" + ("" if ref.startswith("/") else "/") + ref


def is_comix(work: Work) -> bool:
    return bool(work.source and work.source.adapter_key == "comix")


async def descramble_chapter(db: Session, fetcher, work: Work, chapter: Chapter) -> int:
    """Detect + repair scrambled pages in one already-captured comic chapter.

    Returns the number of pages rewritten (0 if the chapter looked clean or nothing was captured).
    The chapter body is updated + recommitted in place; figures for normal pages are untouched."""
    content = chapter.content
    if content is None or not content.body or "comic-page" not in content.body:
        return 0
    srcs = chapter_page_srcs(content.body)
    if not srcs:
        return 0
    url = _reader_url(chapter)
    if not url:
        return 0
    # Always render the reader and capture every page the site descrambles onto a <canvas> — that
    # is the ONLY reliable detector. Seam analysis on the stored bytes (looks_scrambled) is a cheap
    # heuristic that MISSES some series entirely (e.g. One Piece's scramble leaves no 1/5 grid
    # seams), so it can't gate which chapters render or it silently skips real scrambles.
    log.info("descramble work=%s chapter=%s: rendering reader (%d pages)",
             work.id, chapter.index, len(srcs))
    total, captured = await fetcher.capture_canvas(work.source.adapter_key, url)
    if not captured:
        return 0  # no scrambled (canvas) pages → already correct; caller marks it checked
    # Map captured canvas pages onto stored figures by position. Only trust the mapping when the
    # reader's page count matches our stored figure count (otherwise indices would misalign).
    if total != len(srcs):
        log.warning("descramble work=%s chapter=%s: reader pages=%s != stored figures=%s; skipping",
                    work.id, chapter.index, total, len(srcs))
        return 0

    out_dir = descramble_dir(work.id, chapter.id)
    replaced: dict[str, str] = {}  # old /media src -> new descrambled /media url
    for n, png in captured.items():
        if not (1 <= n <= len(srcs)):
            continue
        ext, data = _encode_capture(png)
        fname = f"{n:04d}.{ext}"
        (out_dir / fname).write_bytes(data)
        replaced[srcs[n - 1]] = descramble_url(work.id, chapter.id, fname)
    if not replaced:
        return 0

    def _swap(m: re.Match) -> str:
        new = replaced.get(m.group(2))
        return f"{m.group(1)}{new}{m.group(3)}" if new else m.group(0)

    new_body = _IMG_SRC.sub(_swap, content.body)
    if new_body != content.body:
        import hashlib

        content.body = new_body
        content.checksum = hashlib.sha256(new_body.encode("utf-8")).hexdigest()
        db.commit()
    log.info("descramble work=%s chapter=%s: repaired %d page(s)",
             work.id, chapter.index, len(replaced))
    return len(replaced)
