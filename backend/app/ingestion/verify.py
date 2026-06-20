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

from . import fuzzy
from . import language as lang
from .extract import norm_title

log = logging.getLogger("shelf.verify")

# Book file extensions we can place in the library. EPUB/PDF carry embedded metadata we read;
# the rest fall back to the filename (release files are usually "Author - Title.ext").
_BOOK_EXTS = (".epub", ".pdf", ".azw3", ".azw", ".mobi", ".fb2", ".txt", ".cbz", ".cbr", ".djvu")
# Audiobook audio files (single-file m4b or multi-file mp3/etc.). No cheap embedded-metadata read
# without an audio-tag lib, so audiobook verification leans on the matcher's name-level title gate +
# the file/folder name; the audio is fetched for a KNOWN title, so this is a backstop, not the gate.
_AUDIO_EXTS = (".m4b", ".m4a", ".mp3", ".aac", ".flac", ".ogg", ".opus", ".wma")
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
def _epub_meta(zf: zipfile.ZipFile) -> dict:
    """Read dc:title / dc:creator / dc:language straight from an EPUB's OPF (zip), dependency-free
    and tolerant of malformed books that choke a full parser. Takes an already-open ZipFile so the
    caller can open it straight from the path (reading only the entries it needs) rather than slurping
    the whole file into memory."""
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

    # ISBN from any <dc:identifier> (an EPUB often carries several — uuid, isbn, calibre id); keep the
    # first whose digits are a valid 10/13-length ISBN. An exact ISBN match is the strongest possible
    # confirmation a download is the requested book, so it's worth pulling out of the OPF.
    isbn = None
    for raw in re.findall(r"<dc:identifier\b[^>]*>(.*?)</dc:identifier>", opf, re.I | re.S):
        digits = re.sub(r"[^0-9Xx]", "", html.unescape(raw))
        if len(digits) in (10, 13):
            isbn = digits
            break
    return {"title": tag("title"), "author": tag("creator"), "language": tag("language"), "isbn": isbn}


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


def check_integrity(path: str) -> tuple[bool, str]:
    """Structural integrity of a downloaded book file — NOT just that its metadata is readable. A
    truncated download or a bad-CRC chapter passes the lenient metadata read but breaks at import;
    this catches it up front. Returns (ok, reason).
      * EPUB / CBZ → a valid zip whose every member passes CRC (``testzip``), with an OPF for epub;
      * PDF        → opens and has at least one page;
      * TXT/MD     → non-empty."""
    ext = _ext(path)
    try:
        size = os.path.getsize(path)
        if size == 0:
            return False, "empty file"
        # A 256-byte floor only for container formats (a valid epub/pdf is always far larger zipped);
        # plain text/markdown books can legitimately be tiny.
        if ext in (".epub", ".cbz", ".pdf", ".cbr") and size < 256:
            return False, "file too small for its format"
        if ext in (".epub", ".cbz"):
            with zipfile.ZipFile(path) as zf:
                bad = zf.testzip()           # None when every entry's CRC is good
                if bad is not None:
                    return False, f"corrupt archive entry: {bad}"
                if ext == ".epub" and not any(n.lower().endswith(".opf") for n in zf.namelist()):
                    return False, "epub has no OPF (not a real book)"
            return True, "ok"
        if ext == ".pdf":
            from pypdf import PdfReader
            reader = PdfReader(path)
            if len(reader.pages) < 1:
                return False, "pdf has no pages"
            return True, "ok"
        if ext in (".txt", ".md", ".text", ".cbr"):
            return True, "ok"
        return True, "ok"                    # unknown ext: don't block on integrity we can't assess
    except Exception as exc:  # noqa: BLE001 — any read/parse failure is an integrity failure
        return False, f"unreadable {ext or 'file'}: {str(exc)[:80]}"


def read_book_meta(path: str) -> dict | None:
    """Best-available metadata for a downloaded book file: embedded for EPUB/PDF, else the filename.
    Returns {title, author, language, fmt} or None if the file can't be read at all."""
    ext = _ext(path)
    meta: dict = {}
    try:
        if ext == ".epub":
            try:
                with zipfile.ZipFile(path) as zf:   # reads only the OPF entries, not the whole file
                    meta = _epub_meta(zf)
            except (zipfile.BadZipFile, OSError):
                meta = {}
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


# A title SEGMENT (split on ": " / " - ") that mentions one of these is a SERIES or COLLECTION
# reference, not the book's own title — so a request for "Spellmonger" (book 1, whose title equals
# the series) must not match a DIFFERENT volume/anthology that merely names the series ("Shadowmage:
# Book Nine Of The Spellmonger Series", "The Road To Sevendor - A Spellmonger Anthology").
_REF_MARKS = {
    "series", "saga", "cycle", "chronicles", "chronicle", "sequence", "trilogy",
    "anthology", "collection", "collected", "omnibus", "boxset", "companion", "sampler",
}
_SEG_SPLIT = re.compile(r"\s*[:\-–—]\s+")


def _segments(title: str) -> list[set]:
    """The file title's segments (split on ': ' / ' - '), each as a normalized token set, EXCLUDING
    segments that are a series/collection reference (so only a real book-title segment can match)."""
    out: list[set] = []
    for seg in _SEG_SPLIT.split(title or ""):
        st = set(norm_title(seg).split())
        if st and not (st & _REF_MARKS):
            out.append(st)
    return out


def _seg_score(wt: set, st: set) -> float:
    if not wt or not st:
        return 0.0
    jac = len(wt & st) / len(wt | st)
    if wt <= st or st <= wt:          # containment within a clean segment (subtitle, "Author - Title")
        small, large = sorted((len(wt), len(st)))
        if large and small / large >= 0.5:
            jac = max(jac, 0.9)
    return jac


def _title_score(want: str, got: str) -> float:
    """Best match of the requested title against the file title — scored per clean segment (a
    series/collection mention can't carry the match) with a whole-title Jaccard floor."""
    wt = set(norm_title(want).split())
    gt = set(norm_title(got).split())
    if not wt or not gt:
        return 0.0
    best = len(wt & gt) / len(wt | gt)            # whole-title floor
    for st in _segments(got):
        best = max(best, _seg_score(wt, st))
    return best


def _norm_isbn(s: str | None) -> str:
    """Canonical ISBN-13 form of a 10- or 13-digit ISBN (ISBN-10 is converted to its 13 equivalent),
    so a want/got pair stored in different conventions still compares equal. '' when not an ISBN."""
    d = re.sub(r"[^0-9Xx]", "", str(s or "")).upper()
    if len(d) == 13 and d.isdigit():
        return d
    if len(d) == 10:
        core = "978" + d[:9]
        chk = (10 - sum((1 if i % 2 == 0 else 3) * int(c) for i, c in enumerate(core)) % 10) % 10
        return core + str(chk)
    return ""


def _isbn_match(want_isbns, got_isbn: str | None) -> bool:
    got = _norm_isbn(got_isbn)
    return bool(got) and any(_norm_isbn(w) == got for w in (want_isbns or []))


def score_match(want_title: str, want_author: str | None,
                got_title: str | None, got_author: str | None,
                *, want_titles: list[str] | None = None,
                want_isbns: list | None = None, got_isbn: str | None = None) -> tuple[float, str]:
    """Confidence (0..1) that a file's metadata is the requested book, with a short reason.

    ``want_titles`` are alternate titles (romaji/native/synonyms) — the file's embedded title is
    scored against the best of them, so a book correctly grabbed under its native title isn't failed
    on an English ``dc:title``. ``want_isbns``/``got_isbn``: an exact ISBN match IS the book."""
    # ISBN is the single strongest signal — an exact match short-circuits everything (rescues a
    # correct book whose embedded author is a translator/uploader, or whose title is in another script).
    if _isbn_match(want_isbns, got_isbn):
        return 1.0, "isbn match"
    cand_titles = [t for t in ([want_title, *(want_titles or [])]) if t] or [want_title or ""]
    ts = max((_title_score(t, got_title or "") for t in cand_titles), default=0.0)
    wa, ga = _author_tokens(want_author), _author_tokens(got_author)
    # Fuzzy author so initials ("J.R.R."), name order, and transliteration/OCR variants don't read as
    # a mismatch (the old exact-token-intersection treated "Dostoyevsky" vs "Dostoevsky" as a miss).
    ahit = (fuzzy.author_similarity(want_author or "", got_author or "") >= 0.8) if (wa and ga) else None
    # The requested title can be fully present in a clean segment yet score low (a short title with a
    # long legitimate subtitle, "The Hobbit, or There and Back Again"). Trust it then — but only when
    # the author confirms AND a requested title is wholly inside a non-reference segment, so a
    # different work that merely contains the phrase, or names the series, is not elevated.
    if ahit is True and ts < 0.85:
        for t in cand_titles:
            wt = set(norm_title(t).split())
            if wt and any(wt <= st for st in _segments(got_title or "")):
                ts = max(ts, 0.85)
                break
    score = ts
    if ahit is True:
        score = min(1.0, ts + 0.1)
    elif ahit is False:
        # Embedded author genuinely disagrees (after fuzzy matching ruled out initials/transliteration/
        # order, and ISBN didn't confirm). Against a file's OWN dc:creator this is a strong signal — a
        # same-title different-author study guide or magazine — so it stays strict (×0.5, below the
        # floor). The recall wins come from fuzzy author + ISBN + alternate titles above, NOT from
        # weakening this gate (which live testing showed lets those false positives back in).
        score *= 0.5
    tag = "hit" if ahit is True else ("miss" if ahit is False else "?")
    return round(score, 3), f"title {ts:.2f} · author {tag}"


@dataclass
class CandidateScore:
    score: float
    accept: bool
    reason: str


def score_candidate(meta, cand_title: str | None, cand_author: str | None, *,
                    cand_isbn=None, cand_type: str | None = None,
                    floor: float = 0.5) -> CandidateScore:
    """Pre-download confidence that a search HIT (a libgen/AA card) is the requested work.

    The shared core (``score_match``) does title-vs-every-known-title + graded fuzzy author + ISBN,
    exactly as the post-download gate does — so the AA decision and the usenet/verify decision agree.
    A content-TYPE mismatch (an article/comic when we want prose, or vice-versa) is then applied ONCE
    as a multiplier (``matchmeta.type_compat``); it is NOT folded into ``score_match`` because that
    core is also used against a file's own embedded metadata, which carries no provider type badge.

    ``meta`` is a ``matchmeta.WorkMeta``; its ``raw`` carries an optional ``isbn`` (may be None —
    harmless). Returns score + whether it clears ``floor`` + a short reason."""
    from . import matchmeta as mm
    titles = list(meta.titles) or [""]
    want_isbns = (meta.raw or {}).get("isbn")
    score, reason = score_match(titles[0], meta.author, cand_title, cand_author,
                                want_titles=titles[1:], want_isbns=want_isbns, got_isbn=cand_isbn)
    tc = mm.type_compat(meta.bucket, mm.bucket_of(cand_type))
    if tc < 1.0:
        score = round(score * tc, 3)
        reason = f"{reason} · type ×{tc:g}"
    return CandidateScore(score=score, accept=score >= floor, reason=reason)


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


def find_audio_files(root: str) -> list[str]:
    """Audiobook audio files inside a finished download (file or directory), largest first. Skips
    obvious non-content files (samples/art)."""
    if os.path.isfile(root):
        return [root] if _ext(root) in _AUDIO_EXTS else []
    found: list[tuple[int, str]] = []
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            if _ext(name) not in _AUDIO_EXTS or _SKIP_NAME_RE.search(name):
                continue
            fp = os.path.join(dirpath, name)
            try:
                found.append((os.path.getsize(fp), fp))
            except OSError:
                continue
    found.sort(key=lambda x: x[0], reverse=True)
    return [fp for _s, fp in found]


def verify_audiobook(root: str, want_title: str, want_author: str | None) -> VerifyResult:
    """Light verification of a finished AUDIOBOOK download: there must be ≥1 audio file, and the
    title's words must appear in the download's file/folder names (a name backstop — the matcher
    already gated the release name on title+author confidence). Returns a VerifyResult whose ``path``
    is the audio file when single-file, else the containing directory (multi-file audiobook)."""
    files = find_audio_files(root)
    if not files:
        return VerifyResult(False, 0.0, want_title, want_author, None, "no audio file in download")
    # Title backstop: recall of the title's significant words across the relative path names.
    names = " ".join(os.path.relpath(fp, root) if os.path.isdir(root) else os.path.basename(fp)
                     for fp in files).lower()
    name_toks = set(re.findall(r"[a-z0-9]+", names))
    title_toks = {t for t in norm_title(want_title).split() if len(t) > 1}
    recall = (len(title_toks & name_toks) / len(title_toks)) if title_toks else 1.0
    # LibriVox/archive.org name files as concatenated slugs ("prideandprejudice_01_austen.mp3"), which
    # the token split can't recall — also accept the compact title appearing as a substring.
    compact_title = norm_title(want_title).replace(" ", "")
    compact_names = re.sub(r"[^a-z0-9]+", "", names)
    if compact_title and len(compact_title) >= 5 and compact_title in compact_names:
        recall = max(recall, 1.0)
    ok = recall >= 0.5  # at least half the title words present in the release's filenames
    # Single-file (m4b) → the file; multi-file (mp3 set) → its parent dir, moved/served as a unit.
    parents = {os.path.dirname(fp) for fp in files}
    path = files[0] if len(files) == 1 else (parents.pop() if len(parents) == 1 else root)
    return VerifyResult(ok, round(recall, 3), want_title, want_author, path,
                        f"audiobook · title {recall:.2f}", "audio")


def _epub_text_language(path: str) -> str | None:
    """Best-guess language of an EPUB's actual TEXT (stop-word frequency), used as a fallback when
    the book declares no dc:language. Reads a small sample of the content documents."""
    try:
        zf = zipfile.ZipFile(path)   # open from the path — read only sampled entries, not the whole file
    except Exception:  # noqa: BLE001
        return None
    parts: list[str] = []
    with zf:
        htmls = [n for n in zf.namelist() if n.lower().endswith((".xhtml", ".html", ".htm"))]
        for n in htmls:
            try:
                raw = zf.read(n).decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                continue
            parts.append(re.sub(r"<[^>]+>", " ", raw))   # crude tag strip → text
            if sum(len(p) for p in parts) > 6000:
                break
    return lang.detect_text_language(" ".join(parts))


def file_language(path: str, *, fallback_detect: bool = False) -> str | None:
    """The downloaded file's language (canonical ISO-639-1): embedded dc:language first, then an
    optional content-based guess for EPUBs that declare none."""
    meta = read_book_meta(path) or {}
    code = lang.canonicalize(meta.get("language"))
    if code is None and fallback_detect and _ext(path) == ".epub":
        code = _epub_text_language(path)
    return code


def verify_file(path: str, want_title: str, want_author: str | None,
                *, min_confidence: float = _VERIFY_MIN, want_language: str | None = None,
                want_titles: list[str] | None = None, want_isbns: list | None = None) -> VerifyResult:
    # Integrity FIRST: a corrupt/truncated file is rejected outright (so it's removed + re-downloaded),
    # no matter how well its (lenient) metadata happens to match.
    intact, ireason = check_integrity(path)
    if not intact:
        meta0 = read_book_meta(path) or {}
        return VerifyResult(False, 0.0, meta0.get("title"), meta0.get("author"), path,
                            f"integrity: {ireason}", meta0.get("fmt"))
    meta = read_book_meta(path) or {}
    score, reason = score_match(want_title, want_author, meta.get("title"), meta.get("author"),
                                want_titles=want_titles, want_isbns=want_isbns,
                                got_isbn=meta.get("isbn"))
    # Language verification: if a language was requested and the file's actual language is known and
    # differs, this is the wrong edition — reject it outright (score 0) regardless of title match.
    # An unknown file language is never penalized (don't reject on missing data).
    if want_language:
        flang = file_language(path, fallback_detect=True)
        if flang and flang != want_language:
            return VerifyResult(False, 0.0, meta.get("title"), meta.get("author"), path,
                                f"{reason} · language {flang}≠{want_language}", meta.get("fmt"))
    return VerifyResult(
        ok=score >= min_confidence, confidence=score, title=meta.get("title"),
        author=meta.get("author"), path=path, reason=reason, fmt=meta.get("fmt"),
    )


def verify_download(root: str, want_title: str, want_author: str | None,
                    *, min_confidence: float = _VERIFY_MIN, want_language: str | None = None,
                    want_titles: list[str] | None = None, want_isbns: list | None = None) -> VerifyResult:
    """Best book file in a finished download vs the requested book. ``ok`` is True only when a file
    clears ``min_confidence`` AND (if requested) is in the wanted language — i.e. the content really
    is the book, in the language, we asked for. ``want_titles`` (alternates) and ``want_isbns`` are
    passed through so a book grabbed under a native title / matched by ISBN still verifies."""
    files = find_book_files(root)
    if not files:
        return VerifyResult(False, 0.0, None, None, None, "no book file in download")
    best = None
    for fp in files:
        vr = verify_file(fp, want_title, want_author, min_confidence=min_confidence,
                         want_language=want_language, want_titles=want_titles, want_isbns=want_isbns)
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
