"""Post-download content verification.

A downloaded NZB is only trustworthy once we've looked inside it. This reads a finished download's
actual book files — their EMBEDDED metadata (EPUB OPF title/creator, PDF info), not just the release
name — and scores them against the book we asked for. That's what lets the fetcher safely grab
lower-confidence (speculative) releases: download fast, verify the content, keep it only if it really
is the requested book; otherwise mark the release broken and try the next candidate.

Multi-book aware: a pack/omnibus download contains several files, so ``match_titles`` maps a set of
wanted titles to their best matching file (one boxset can fulfil several series volumes).
"""
from __future__ import annotations

import html
import io
import logging
import os
import re
import zipfile
from dataclasses import dataclass

from .extract import norm_title

log = logging.getLogger("shelf.verify")

# Book file extensions we can place in the library. EPUB/PDF carry embedded metadata we read;
# the rest fall back to the filename (release files are usually "Author - Title.ext").
_BOOK_EXTS = (".epub", ".pdf", ".azw3", ".azw", ".mobi", ".fb2", ".txt", ".cbz", ".cbr", ".djvu")
# Files inside a download that are never the book (samples, scene junk, art, archives).
_SKIP_NAME_RE = re.compile(r"(?:sample|proof|reader\s*group|\bnfo\b|readme|cover|thumbs)", re.I)
_VERIFY_MIN = 0.6   # default content-match floor


@dataclass
class VerifyResult:
    ok: bool
    confidence: float
    title: str | None
    author: str | None
    path: str | None
    reason: str
    fmt: str | None = None


def _ext(path: str) -> str:
    base = path.lower()
    dot = base.rfind(".")
    return base[dot:] if dot >= 0 else ""


def _stem(path: str) -> str:
    base = path.replace("\\", "/").rsplit("/", 1)[-1]
    dot = base.rfind(".")
    return base[:dot] if dot > 0 else base


# ------------------------------------------------------------------ embedded metadata
def _epub_meta(data: bytes) -> dict:
    """Read dc:title / dc:creator / dc:language straight from an EPUB's OPF (zip), dependency-free
    and tolerant of malformed books that choke a full parser."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except Exception:  # noqa: BLE001 — not a valid zip / truncated download
        return {}
    with zf:
        names = zf.namelist()
        opf_name = None
        if "META-INF/container.xml" in names:
            try:
                cont = zf.read("META-INF/container.xml").decode("utf-8", "replace")
                m = re.search(r'full-path="([^"]+\.opf)"', cont)
                if m:
                    opf_name = m.group(1)
            except Exception:  # noqa: BLE001
                opf_name = None
        if not opf_name:
            opf_name = next((n for n in names if n.lower().endswith(".opf")), None)
        if not opf_name:
            return {}
        try:
            opf = zf.read(opf_name).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            return {}

    def tag(name: str) -> str | None:
        m = re.search(rf"<dc:{name}\b[^>]*>(.*?)</dc:{name}>", opf, re.I | re.S) \
            or re.search(rf"<{name}\b[^>]*>(.*?)</{name}>", opf, re.I | re.S)
        if not m:
            return None
        return html.unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip() or None

    return {"title": tag("title"), "author": tag("creator"), "language": tag("language")}


def _pdf_meta(data: bytes) -> dict:
    try:
        from pypdf import PdfReader
        info = PdfReader(io.BytesIO(data)).metadata or {}
        return {
            "title": (getattr(info, "title", None) or None),
            "author": (getattr(info, "author", None) or None),
            "language": None,
        }
    except Exception:  # noqa: BLE001
        return {}


def read_book_meta(path: str) -> dict | None:
    """Best-available metadata for a downloaded book file: embedded for EPUB/PDF, else the filename.
    Returns {title, author, language, fmt} or None if the file can't be read at all."""
    ext = _ext(path)
    meta: dict = {}
    try:
        if ext == ".epub":
            with open(path, "rb") as f:
                meta = _epub_meta(f.read())
        elif ext == ".pdf":
            with open(path, "rb") as f:
                meta = _pdf_meta(f.read())
    except OSError:
        return None
    if not meta.get("title"):
        # Filenames are commonly "Author - Title" / "Title - Author"; the title-scorer tolerates the
        # extra author tokens (containment), so the whole stem is a safe fallback signal.
        meta["title"] = _stem(path)
    meta["fmt"] = ext.lstrip(".") or None
    return meta


# ------------------------------------------------------------------ matching
def _author_tokens(author: str | None) -> set[str]:
    if not author:
        return set()
    toks: set[str] = set()
    for part in re.split(r"[,&;]| and ", author.lower()):
        for t in re.split(r"[\s._\-]+", part):
            t = t.strip()
            if len(t) >= 2 and not t.endswith("."):
                toks.add(t)
    return toks


def _title_score(want: str, got: str) -> float:
    wt = set(norm_title(want).split())
    gt = set(norm_title(got).split())
    if not wt or not gt:
        return 0.0
    jac = len(wt & gt) / len(wt | gt)
    # Containment boost — but only when the shorter title is a LARGE fraction of the longer (a tight
    # match: subtitle, "Author - Title"). A loose containment, where the file title merely contains
    # the requested phrase amid many other words (a magazine "Heated Rivalry: Inside TV's Hottest
    # Show…"), is a DIFFERENT work and must not be boosted.
    if wt <= gt or gt <= wt:
        small, large = sorted((len(wt), len(gt)))
        if large and small / large >= 0.5:
            jac = max(jac, 0.9)
    return jac


def score_match(want_title: str, want_author: str | None,
                got_title: str | None, got_author: str | None) -> tuple[float, str]:
    """Confidence (0..1) that a file's metadata is the requested book, with a short reason."""
    ts = _title_score(want_title or "", got_title or "")
    wa, ga = _author_tokens(want_author), _author_tokens(got_author)
    ahit = bool(wa & ga) if (wa and ga) else None
    # The requested title can be fully present yet score low under the tight-containment rule when the
    # file carries a long legitimate subtitle ("The Hobbit, or There and Back Again"). Trust it then —
    # but ONLY when the author also confirms, so a longer DIFFERENT work that merely contains the
    # phrase (a magazine, or "It" vs "It Ends With Us") is not elevated.
    if ahit is True and ts < 0.85:
        wt, gt = set(norm_title(want_title or "").split()), set(norm_title(got_title or "").split())
        if wt and wt <= gt:
            ts = max(ts, 0.85)
    score = ts
    if ahit is True:
        score = min(1.0, ts + 0.1)
    elif ahit is False:
        score *= 0.5                  # title matches but author disagrees → likely a different book
    tag = "hit" if ahit is True else ("miss" if ahit is False else "?")
    return round(score, 3), f"title {ts:.2f} · author {tag}"


def find_book_files(root: str) -> list[str]:
    """Supported book files inside a finished download (file or directory), largest first (the main
    book usually dwarfs samples/extras). Obvious non-book files are skipped."""
    if os.path.isfile(root):
        return [root] if _ext(root) in _BOOK_EXTS else []
    found: list[tuple[int, str]] = []
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            if _ext(name) not in _BOOK_EXTS or _SKIP_NAME_RE.search(name):
                continue
            fp = os.path.join(dirpath, name)
            try:
                size = os.path.getsize(fp)
            except OSError:
                continue
            found.append((size, fp))
    found.sort(key=lambda x: x[0], reverse=True)
    return [fp for _s, fp in found]


def verify_file(path: str, want_title: str, want_author: str | None,
                *, min_confidence: float = _VERIFY_MIN) -> VerifyResult:
    meta = read_book_meta(path) or {}
    score, reason = score_match(want_title, want_author, meta.get("title"), meta.get("author"))
    return VerifyResult(
        ok=score >= min_confidence, confidence=score, title=meta.get("title"),
        author=meta.get("author"), path=path, reason=reason, fmt=meta.get("fmt"),
    )


def verify_download(root: str, want_title: str, want_author: str | None,
                    *, min_confidence: float = _VERIFY_MIN) -> VerifyResult:
    """Best book file in a finished download vs the requested book. ``ok`` is True only when a file
    clears ``min_confidence`` — i.e. the content really is the book we asked for."""
    files = find_book_files(root)
    if not files:
        return VerifyResult(False, 0.0, None, None, None, "no book file in download")
    best = None
    for fp in files:
        vr = verify_file(fp, want_title, want_author, min_confidence=min_confidence)
        if best is None or vr.confidence > best.confidence:
            best = vr
    return best


def match_titles(root: str, wanted: list[tuple], *, min_confidence: float = _VERIFY_MIN) -> dict:
    """Map several wanted books to their best matching file in a multi-book download.

    ``wanted``: list of (key, title, author). Returns {key: VerifyResult} for each wanted book that
    has a file clearing ``min_confidence`` — so one pack/omnibus download fulfils multiple volumes.
    Each file is assigned to at most one wanted book (its best claimant)."""
    files = [(fp, read_book_meta(fp) or {}) for fp in find_book_files(root)]
    # Score every (wanted, file) pair, then greedily assign best pairs first so two volumes don't
    # both claim the same file.
    pairs: list[tuple[float, str, str, VerifyResult]] = []
    for key, t, a in wanted:
        for fp, meta in files:
            score, reason = score_match(t, a, meta.get("title"), meta.get("author"))
            if score >= min_confidence:
                pairs.append((score, key, fp, VerifyResult(
                    True, score, meta.get("title"), meta.get("author"), fp, reason, meta.get("fmt"))))
    pairs.sort(key=lambda x: x[0], reverse=True)
    out: dict = {}
    used: set[str] = set()
    for _score, key, fp, vr in pairs:
        if key in out or fp in used:
            continue
        out[key] = vr
        used.add(fp)
    return out
