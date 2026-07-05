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

import asyncio
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


# A page whose height/width exceeds this is a webtoon/manhua "long strip", not a manga page. The
# reader draws those onto a single very-tall <canvas> that never finishes painting in the headless
# capture context — so capturing them yields an empty (dark) image. We skip descrambling such
# chapters entirely (leave the originals) rather than butcher them.
_LONG_STRIP_ASPECT = 2.4
# A captured canvas must carry real content. A failed/half-painted capture is near-uniform (very
# low pixel variance) and dark; a real page has high variance. Verified: good manga capture
# std≈105 / mean≈155, garbage manhua capture std≈8 / mean≈43.
_MIN_CAPTURE_STD = 22.0
_MIN_CAPTURE_MEAN = 30.0


def _page_aspect(path) -> float | None:
    """Height/width of a stored page image (PIL reads only the header). None if unreadable."""
    try:
        from PIL import Image
        with Image.open(path) as im:
            w, h = im.size
        return (h / w) if w else None
    except Exception:
        return None


def is_long_strip(srcs: list[str]) -> bool:
    """True when a chapter's pages are predominantly tall vertical strips (webtoon/manhua format),
    which the canvas-capture descrambler can't render. Uses the median page aspect, skipping the
    first page (often a short credits/title banner) so it doesn't skew the result."""
    aspects = [a for s in srcs for a in (_page_aspect(_local_path(s)),) if a is not None]
    body = aspects[1:] if len(aspects) > 2 else aspects  # drop the banner-ish first page
    if not body:
        return False
    body.sort()
    median = body[len(body) // 2]
    return median >= _LONG_STRIP_ASPECT


def _capture_is_valid(png: bytes) -> bool:
    """Reject an empty / half-painted capture (near-uniform, dark) so we never replace a real page
    with garbage — the failure mode for long strips and slow-loading canvases."""
    try:
        import io

        import numpy as np
        from PIL import Image

        with Image.open(io.BytesIO(png)) as im:
            a = np.asarray(im.convert("L"), dtype=np.float32)
    except Exception:
        return False
    return float(a.std()) >= _MIN_CAPTURE_STD and float(a.mean()) >= _MIN_CAPTURE_MEAN


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


class DescrambleIncomplete(Exception):
    """The reader render was incomplete (0 pages hydrated, or a page-count mismatch), so we can't
    tell whether the chapter is scrambled. Distinct from "no scrambled pages": the caller must
    RETRY rather than stamp the chapter checked — a transiently-slow/blocked render must not
    permanently leave a scrambled chapter in place (CC1)."""


async def descramble_chapter(db: Session, fetcher, work: Work, chapter: Chapter) -> int:
    """Detect + repair scrambled pages in one already-captured comic chapter.

    Returns the number of pages rewritten (0 if the chapter looked clean / nothing was scrambled).
    Raises DescrambleIncomplete when the render was incomplete (retry, do NOT mark checked).
    The chapter body is updated + recommitted in place; figures for normal pages are untouched."""
    content = chapter.content
    if content is None or not content.body or "comic-page" not in content.body:
        return 0
    srcs = chapter_page_srcs(content.body)
    if not srcs:
        return 0
    # Long-strip formats (webtoon/manhua) render each scrambled page onto a single very-tall canvas
    # that the headless reader never finishes painting, so a capture would just be a dark image. The
    # descrambler can't help these — skip them (the original stays) rather than butcher them. (comix
    # scrambles a fixed early page subset; the rest of the strip is normal and untouched anyway.)
    if is_long_strip(srcs):
        log.info("descramble work=%s chapter=%s: long-strip (webtoon/manhua) — skipping",
                 work.id, chapter.index)
        return 0
    url = _reader_url(chapter)
    if not url:
        return 0
    # Render the reader and capture every page the site descrambles onto a <canvas> — that is the
    # ONLY reliable detector. Seam analysis on the stored bytes (looks_scrambled) is a cheap
    # heuristic that MISSES some series entirely (e.g. One Piece's scramble leaves no 1/5 grid
    # seams), so it can't gate which chapters render or it silently skips real scrambles.
    log.info("descramble work=%s chapter=%s: rendering reader (%d pages)",
             work.id, chapter.index, len(srcs))
    total, captured = await fetcher.capture_canvas(work.source.adapter_key, url)
    if total == 0:
        # The reader never hydrated ANY page (slow render / CF interstitial / nav failure). This is
        # NOT "no scrambled pages" — raise so the chapter is retried instead of being stamped
        # checked with its (possibly scrambled) pages left in place.
        raise DescrambleIncomplete(f"reader rendered 0 pages for chapter {chapter.index}")
    if not captured:
        return 0  # pages hydrated, none on a <canvas> → nothing was scrambled → genuinely done
    # Map captured canvas pages onto stored figures by position. Only trust the mapping when the
    # reader's page count matches our stored figure count (otherwise indices would misalign).
    if total != len(srcs):
        # A PARTIAL render (mismatched page count) — can't safely map captures → retry, don't stamp.
        raise DescrambleIncomplete(
            f"reader pages={total} != stored figures={len(srcs)} (partial render)")

    out_dir = descramble_dir(work.id, chapter.id)

    def _render_pages() -> dict[str, str]:
        # PIL decode + validity check + WebP method=6 encode + file writes are CPU/IO-heavy; run the
        # whole per-page loop OFF the event loop so a multi-page chapter doesn't freeze the reader and
        # every other scheduler tick (this runs inside the crawl_tick gather).
        out: dict[str, str] = {}  # old /media src -> new descrambled /media url
        for n, png in captured.items():
            if not (1 <= n <= len(srcs)):
                continue
            # Never replace a real page with a failed/empty capture (long-strip / slow-load garbage).
            if not _capture_is_valid(png):
                log.warning("descramble work=%s chapter=%s: page %s capture invalid (empty/dark) — keeping original",
                            work.id, chapter.index, n)
                continue
            ext, data = _encode_capture(png)
            fname = f"{n:04d}.{ext}"
            (out_dir / fname).write_bytes(data)
            out[srcs[n - 1]] = descramble_url(work.id, chapter.id, fname)
        return out

    replaced = await asyncio.to_thread(_render_pages)
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
