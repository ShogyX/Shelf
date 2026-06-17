"""Match a catalog book to Prowlarr usenet releases.

Given a book (title, author, language, media kind) and the candidate releases a Prowlarr search
returns, parse each release name (format / language / edition / ebook-vs-audiobook), score it
against the book, dedup duplicate releases, and apply a strict confidence gate so fully-automatic
grabbing never fetches the wrong book. Pure parsing/scoring lives here (unit-tested); the live
Prowlarr search + grab orchestration build on it.
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import CatalogWork, Integration
from . import fuzzy
from . import language as lang
from .broken import broken_keys, release_key
from .extract import norm_title

log = logging.getLogger("shelf.release_matcher")

# Confidence floors. A release must clear MATCH_FLOOR to be a candidate at all; AUTO_GRAB_DEFAULT
# is the (configurable) bar for fully-automatic grabbing. Strict by design.
MATCH_FLOOR = 0.6
AUTO_GRAB_DEFAULT = 0.8
# When the book has NO author, confidence is title-only and an author can't disambiguate, so a
# weak/partial title match ("Telling Lies" → "Telling Lies About Hitler") is a false positive that
# wastes a grab+download. Demand a near-exact title in that case. This also rejects catalog rows
# whose "title" is actually an author name (no author field, title 0.00 vs the real book title).
NO_AUTHOR_MIN_CONF = 0.9
# When the work has no author BUT a non-canonical ALTERNATE title matches at least this strongly, the
# alt-title hit is treated as disambiguating evidence and the author-less floor drops to this value
# (comic-level). Set above MATCH_FLOOR so a partial title can't sneak through — it must be a strong,
# specific alt-title match (e.g. a work's native/translated title appearing in the release name).
ALT_TITLE_MIN_CONF = 0.8

# Every ebook container we recognize when PARSING a release name (so the format token is stripped
# from the content tokens). Recognition ≠ usability — see IMPORTABLE_FORMATS.
EBOOK_FORMATS = ["epub", "azw3", "azw", "mobi", "kf8", "fb2", "pdf", "djvu", "lit", "cbz", "cbr"]
_EBOOK_SET = set(EBOOK_FORMATS)
# Formats the importer can actually turn into a Work (mirrors local_folder/media.is_supported:
# epub/pdf/txt + comic cbz/cbr). We have no calibre, so azw3/azw/mobi/kf8/fb2/lit/djvu can't be
# ingested — grabbing one just wastes a download and fails verify. This is the DEFAULT preferred
# list; an operator who installs a converter can override `preferred_formats` to add others.
IMPORTABLE_FORMATS = ["epub", "pdf", "txt", "cbz", "cbr"]
_AUDIO_FORMATS = {"m4b", "m4a", "mp3", "flac", "aac", "ogg", "opus"}
_AUDIO_HINTS = {"audiobook", "audiobooks", "unabridged", "abridged", "audio"}
_EDITION_TOKENS = {
    "retail", "proper", "repack", "revised", "annotated", "illustrated", "deluxe",
    "anniversary", "collectors", "collector", "definitive", "uncensored",
}
# A multi-work bundle: grabbing one of these when a single title is wanted is a WRONG edition,
# so they never auto-grab (still listed as candidates).
_BOXSET_TOKENS = {
    "omnibus", "boxset", "boxsets", "box", "collection", "collected", "anthology", "trilogy",
    "duology", "tetralogy", "saga", "compendium", "bundle", "complete",
}
# NOT bare "set"/"books": they're ordinary title words ("Set Me Free", "Books of Blood") and flagging
# them blocked auto-grab of real single books. Genuine bundles still trip via "box"/"boxset"/the
# numeric-range regex ("Books 1-3" → range match), so precision is unchanged where it matters.
# A companion product (summary/study guide/artbook/…) or a periodical (magazine/newspaper), never
# the work itself → hard reject.
_COMPANION_TOKENS = {
    "summary", "summaries", "analysis", "guide", "guides", "study", "studyguide", "workbook",
    "sparknotes", "cliffsnotes", "companion", "fanbook", "artbook", "databook", "sourcebook",
    "handbook", "encyclopedia", "conversation", "takeaways", "review", "notes", "outline",
    "magazine", "magazines", "periodical", "newspaper",
}
# Tokens that are noise for title/author matching (formats, media words, scene cruft).
_NOISE_TOKENS = (
    _EBOOK_SET | _AUDIO_FORMATS | _AUDIO_HINTS | _EDITION_TOKENS | {
        "ebook", "ebooks", "book", "novel", "the", "a", "an", "of", "and",
        "retail", "scene", "web", "edition", "vol", "volume", "read", "audiobook",
    }
)
# Short connective words that don't count as "unexplained" content for the precision gate.
_STOPWORDS = {"the", "a", "an", "of", "and", "or", "to", "in", "for", "s", "his", "her", "novel"}
_ROMAN_RE = re.compile(r"^(?=[mdclxvi])m{0,3}(cm|cd|d?c{0,3})(xc|xl|l?x{0,3})(ix|iv|v?i{0,3})$")
_VOL_RE = re.compile(
    r"(?:\b(?:book|bk|vol|volume|part|pt|#)\s*0*(\d{1,3})\b)|(?:\bbooks?\s*0*\d{1,3}\s*[-–]\s*0*\d{1,3}\b)",
    re.I,
)
_LANG_TOKENS = {
    "english": "en", "eng": "en", "german": "de", "deutsch": "de", "ger": "de",
    "french": "fr", "francais": "fr", "fre": "fr", "spanish": "es", "espanol": "es", "spa": "es",
    "italian": "it", "ita": "it", "portuguese": "pt", "russian": "ru", "japanese": "ja",
    "chinese": "zh", "korean": "ko", "dutch": "nl", "polish": "pl", "swedish": "sv",
}
_YEAR_RE = re.compile(r"^(19|20)\d{2}$")
_SPLIT_RE = re.compile(r"[\s._\-\[\]()+#,;:'\"&]+")
_RANGE_RE = re.compile(r"\b\d{1,3}\s*[-–]\s*\d{1,3}\b")

# Junk-release rejection (ported from Radarr ValidateBeforeParsing / RejectHashedReleasesRegex):
# obfuscated/hashed single-token names that carry no real title.
_HASHED_RES = [re.compile(p, re.I) for p in (
    r"^[0-9a-z]{32}$", r"^[a-z0-9]{24}$", r"^[a-z]{11}\d{3}$", r"^[a-z]{12}\d{3}$",
    r"^[0-9a-f]{40}$", r"^[0-9]{30,}$",
)]
# Site prefixes / tracker suffixes to strip before tokenizing (don't pollute the title tokens).
# Only a leading bracket that looks like a DOMAIN ("[NovelBin.com]") or a www. prefix — never a
# generic bracket like "[ITA]" / "[M4B]" (those are real language/format tags).
_SITE_PREFIX_RE = re.compile(
    r"^\s*(?:\[[^\]]*\.[a-z]{2,}[^\]]*\]|\(?www\.[^\s)]+\)?)\s*[-_.]*\s*", re.I)
_TRACKER_SUFFIX_RE = re.compile(
    r"\s*[\[(](?:ettv|rarbg|eztv|tgx|rartv|public|[a-z0-9.\-]+\.(?:com|net|org|to|io))[\])]\s*$", re.I)
# Proper / repack / version (revision) markers — a corrected re-release, mildly preferred.
_PROPER_RE = re.compile(r"\b(proper|repack|rerip|real)\b", re.I)
_VERSION_RE = re.compile(r"\b(?:v|version)\s*([2-9])\b|\[v([2-9])\]", re.I)


def _strip_affixes(title: str) -> str:
    t = _SITE_PREFIX_RE.sub("", title or "")
    t = _TRACKER_SUFFIX_RE.sub("", t)
    return t.strip()


def _looks_hashed(title: str) -> bool:
    """A junk release with no real title: password-protected spam, all-symbol, or a single hash."""
    t = (title or "").strip()
    if re.search(r"\bpassword\b", t, re.I) and re.search(r"\byenc\b", t, re.I):
        return True
    core = re.sub(r"\.[a-z0-9]{2,4}$", "", t, flags=re.I)  # drop a file extension
    if not re.search(r"[^\W_]", core):                     # no letter/digit at all
        return True
    toks = [x for x in _SPLIT_RE.split(core) if x]
    return len(toks) == 1 and any(rx.match(toks[0]) for rx in _HASHED_RES)


@dataclass
class ReleaseInfo:
    fmt: str | None                       # ebook container, or "audio" for audiobooks, or None
    is_audiobook: bool
    language: str | None                  # primary (trailing) language code parsed from the name
    languages: set[str] = field(default_factory=set)  # ALL languages the name declares
    multi_lang: bool = False              # a "MULTi" release or >1 declared language
    editions: set[str] = field(default_factory=set)
    is_retail: bool = False
    content_tokens: set[str] = field(default_factory=set)  # title/author tokens, noise stripped
    content_seq: tuple = ()               # same, in order (order-preserving dedup key)
    is_boxset: bool = False               # omnibus/boxset/trilogy/"books 1-3" → never auto-grab
    is_companion: bool = False            # summary/study-guide/artbook → reject outright
    volume: int | None = None             # a single declared volume number, if any
    group: str | None = None              # scene release-group tag (after the final hyphen)
    raw_tokens: tuple = ()                # all tokens (for the title-aware precision gate)
    is_junk: bool = False                 # hashed/obfuscated/password-spam release → reject
    is_proper: bool = False               # proper/repack/real → a corrected re-release
    version: int = 1                      # release version (v2/v3…), 1 if unmarked


def _author_tokens(author: str | None) -> set[str]:
    if not author:
        return set()
    # Authors come as "Last, First" or "First Last" (possibly several, comma/&-joined).
    raw = re.split(r"[,&;]| and ", author.lower())
    toks: set[str] = set()
    for part in raw:
        for t in _SPLIT_RE.split(part):
            t = t.strip()
            if len(t) >= 2 and not t.endswith("."):
                toks.add(t)
    return toks


def _author_fuzzy_hit(author_toks: set[str], rel: set[str]) -> bool:
    """A release content token that's a transliteration/OCR variant of an author token
    ("tolkein" for "tolkien") counts as the author being present, even with no exact token hit.
    Restricted to discriminating (>=4 char) tokens so short common words can't trigger it."""
    longs = [t for t in author_toks if len(t) >= 4]
    rels = [t for t in rel if len(t) >= 4]
    # 85 catches single-transposition transliterations ("tolkien"/"tolkein") while rejecting
    # genuinely different surnames ("wilson"/"watson" ≈ 67). This only gates whether the candidate is
    # tried + content-verified, so post-download verify is the real backstop against a false hit.
    return any(fuzzy.ratio(a, r) >= 85 for a in longs for r in rels)


def parse_release(title: str, categories: list[int] | None = None) -> ReleaseInfo:
    """Parse a usenet release name into structured matching signals. ``categories`` (newznab ids)
    disambiguate audiobooks (3030) from ebooks when the name is ambiguous."""
    orig = str(title or "")
    is_junk = _looks_hashed(orig)
    is_proper = bool(_PROPER_RE.search(orig))
    vm = _VERSION_RE.search(orig)
    version = int(next((g for g in (vm.groups() if vm else ()) if g), 1)) if vm else 1
    # Strip site/tracker affixes so they don't pollute the title tokens.
    stripped = _strip_affixes(orig)
    raw = stripped.lower()
    toks = [t for t in _SPLIT_RE.split(raw) if t]
    tokset = set(toks)

    cats = set(categories or [])
    is_audiobook = bool(
        3030 in cats or (_AUDIO_FORMATS & tokset) or (_AUDIO_HINTS & tokset)
    )
    fmt: str | None = None
    if is_audiobook:
        fmt = "audio"
    else:
        for t in toks:                    # first recognized ebook container wins
            if t in _EBOOK_SET:
                fmt = t
                break

    # Language: a full multi-pass parse (Radarr/Sonarr LanguageParser-style) on the ORIGINAL-case
    # string so the case-sensitive 2-letter code pass (…DE…/…FR…) fires. `language` is the primary
    # (last-occurring) tag so a title word ("The German Wife") doesn't override a real trailing
    # "…German"; `languages` is the full declared set (multi-language is first-class).
    languages = lang.detect_languages(stripped)
    language = lang.primary_language(stripped)
    multi_lang = lang.is_multi_language(stripped)

    is_boxset = bool(_BOXSET_TOKENS & tokset) or bool(_RANGE_RE.search(raw))
    is_companion = bool(_COMPANION_TOKENS & tokset)
    vol_m = _VOL_RE.search(raw)
    volume = int(vol_m.group(1)) if (vol_m and vol_m.group(1)) else None

    # Scene release-group tag: the single alnum run after the FINAL hyphen (…eBook-BitBook). Only
    # when that hyphen is scene-style — NO space before it. A spaced " - " is the common
    # "Author - Series NN - Title" separator where the trailing run is the TITLE, not a group;
    # stripping it there would zero out title recall (so a clean epub scores 0 and loses to junk).
    group = None
    gm = re.search(r"(?<=\S)-([a-z0-9]{2,})$", raw)
    if gm:
        group = gm.group(1)

    seq = [
        t for t in toks
        if t not in _NOISE_TOKENS and not _YEAR_RE.match(t) and t not in _LANG_TOKENS
        and t not in _AUDIO_FORMATS and t not in _EBOOK_SET and t not in _BOXSET_TOKENS
        and t not in _COMPANION_TOKENS and len(t) > 1 and t != group
    ]
    return ReleaseInfo(
        fmt=fmt, is_audiobook=is_audiobook, language=language,
        languages=languages, multi_lang=multi_lang,
        editions=_EDITION_TOKENS & tokset, is_retail=("retail" in tokset),
        content_tokens=set(seq), content_seq=tuple(seq),
        is_boxset=is_boxset, is_companion=is_companion, volume=volume,
        group=group, raw_tokens=tuple(toks),
        is_junk=is_junk, is_proper=is_proper, version=version,
    )


def title_author_confidence(book_title: str, book_author: str | None, info: ReleaseInfo) -> float:
    """How confidently this release is the given book: recall of the book's title tokens within the
    release, gated by author presence. Recall (not Jaccard) because a release name carries lots of
    extra tokens (author, year, format, group) the title doesn't."""
    title_toks = set(norm_title(book_title).split())
    # Drop from the DENOMINATOR exactly what the release tokenizer strips from content_tokens as
    # noise (_NOISE_TOKENS: function words AND media words like "book"/"novel"/"story"/"ebook").
    # Using the smaller _STOPWORDS here was asymmetric: a title like "The Book Thief" kept {book,
    # thief} while the release only exposed {thief}, capping recall at 0.5 and rejecting an exact
    # match. Strip the same noise set on both sides so recall is computed over real title words.
    sig = title_toks - _NOISE_TOKENS or title_toks - _STOPWORDS or title_toks
    rel = info.content_tokens
    if not sig or not rel:
        return 0.0
    title_toks = sig
    recall = len(title_toks & rel) / len(title_toks)
    if recall == 0.0:
        return 0.0
    author_toks = _author_tokens(book_author)
    # author_hit: True = the author is visibly present in the release name (exact token, or a
    # transliteration/OCR variant — "Tolkein" for "Tolkien"); False = a known author that doesn't
    # appear; None = the work has no author to check. Release names ROUTINELY omit the author, so a
    # False here is weak evidence (it does NOT mean a wrong author), and is penalised only lightly.
    author_hit = (bool(author_toks & rel) or _author_fuzzy_hit(author_toks, rel)) if author_toks else None
    score = recall
    # A single-token title is dangerous (e.g. "It", "Dune") — require an author hit to trust it.
    if len(title_toks) < 2:
        if author_hit is not True:
            return 0.0
        score = 1.0
    elif author_hit is False:
        # Author not visible in the release NAME. Names commonly omit authors, so keep this a
        # SPECULATIVE candidate (tried + content-verified post-download) rather than near-rejecting
        # it: 0.75 stays above the cascade floor yet below the auto-grab bar, so a perfect-title
        # release is downloaded and its EMBEDDED metadata/ISBN — not the name — makes the call.
        score *= 0.75
    return min(score, 1.0)


def get_prowlarr(db: Session) -> Integration | None:
    """The enabled Prowlarr integration (search source), if configured."""
    return db.scalar(
        select(Integration).where(Integration.kind == "prowlarr", Integration.enabled.is_(True))
    )


import functools


@functools.lru_cache(maxsize=512)
def _term_matcher(term: str):
    """A matcher for a required/ignored/preferred term: '/pattern/flags' → regex (i/m/s/x flags),
    else a case-insensitive substring (Radarr's TermMatcher convention)."""
    term = (term or "").strip()
    m = re.fullmatch(r"/(.+)/([imsx]*)", term)
    if m:
        flags = 0
        for ch in m.group(2):
            flags |= {"i": re.I, "m": re.M, "s": re.S, "x": re.X}[ch]
        try:
            rx = re.compile(m.group(1), flags)
        except re.error:
            return lambda name: False
        return lambda name: bool(rx.search(name or ""))
    low = term.lower()
    return (lambda name: low in (name or "").lower()) if low else (lambda name: False)


def _compile_terms(terms) -> list:
    return [_term_matcher(t) for t in (terms or []) if (t or "").strip()]


# Comic/manga search defaults: usenet files them under Newznab 7030 (Books/Comics), packaged as
# CBZ/CBR — distinct from the ebook categories/formats, so a comic search must use its own.
COMIC_CATEGORIES = [7030]
COMIC_FORMATS = ["cbz", "cbr"]


def search_prefs(integ: Integration | None, *, media_kind: str = "text") -> dict:
    """Read search/filter preferences off the Prowlarr integration config, with defaults — tuned to
    the work's ``media_kind`` so a COMIC searches comic categories (7030) for CBZ/CBR releases, while
    prose searches the ebook categories. Operators can override either set in the integration config
    (``comic_categories`` / ``comic_formats`` for comics, ``categories`` / ``preferred_formats`` for
    prose)."""
    cfg = (integ.config if integ else None) or {}
    is_comic = media_kind == "comic"
    if is_comic:
        cats = cfg.get("comic_categories") or COMIC_CATEGORIES
        formats = [f.lower() for f in (cfg.get("comic_formats") or COMIC_FORMATS)]
        # Comic volumes vary wildly in size (a few MB → hundreds); the ebook size gate would wrongly
        # reject them, so it's off unless the operator sets a comic-specific bound.
        min_size, max_size = cfg.get("comic_min_size_mb"), cfg.get("comic_max_size_mb")
        want_audiobooks, want_ebooks = False, True
    else:
        cats = cfg.get("categories") or [7000, 7020]
        formats = [f.lower() for f in (cfg.get("preferred_formats") or IMPORTABLE_FORMATS)]
        min_size, max_size = cfg.get("min_size_mb"), cfg.get("max_size_mb")
        want_audiobooks = 3030 in cats
        # Ebooks/comics wanted unless the operator restricted to audiobooks ONLY — so an unusual
        # category set never silently disables the format gate (which would let anything through).
        want_ebooks = (set(cats) != {3030})
    return {
        "categories": cats,
        "is_comic": is_comic,
        "indexer_ids": cfg.get("indexer_ids") or None,
        "protocols": tuple(cfg.get("protocols") or ("usenet",)),
        "preferred_formats": formats,
        "languages": [l.lower() for l in (cfg.get("languages") or [])],
        "min_size_mb": min_size,
        "max_size_mb": max_size,
        "exclude_terms": [t.lower() for t in (cfg.get("exclude_terms") or [])],
        # Required: the release must contain ≥1 (hard gate). Ignored: must contain 0 (hard gate).
        # Preferred: matches add to the rank score. Each term: '/regex/flags' or a substring.
        "required_terms": list(cfg.get("required_terms") or []),
        "ignored_terms": list(cfg.get("ignored_terms") or []),
        "preferred_terms": list(cfg.get("preferred_terms") or []),
        "want_audiobooks": want_audiobooks,
        "want_ebooks": want_ebooks,
        "auto_grab_min_confidence": float(cfg.get("auto_grab_min_confidence", AUTO_GRAB_DEFAULT)),
        # A single-title request never wants a boxset/omnibus: even if it downloads, the verify step
        # rejects the multi-work bundle, so accepting it just burns a download+verify cycle. Reject
        # them outright unless an operator opts in (a path that legitimately wants bundles).
        "allow_boxsets": bool(cfg.get("allow_boxsets", False)),
    }


@dataclass
class ScoredRelease:
    release: object                 # the prowlarr.Release
    info: ReleaseInfo
    confidence: float               # title/author confidence (0..1)
    score: float                    # overall rank score
    accepted: bool                  # clears the floor + passes format/language/size/exclude gates
    auto_ok: bool                   # eligible for fully-automatic grab (strict gate)
    reason: str                     # short human explanation


def _release_bucket(release) -> str:
    """Coarse type bucket of a usenet release from its newznab categories (7030 = Comics,
    7000/7020 = Books, a 'Magazine'/'Comic' category name) — used to down-rank a cross-typed result."""
    from . import matchmeta as mm
    ids = set(getattr(release, "categories", None) or [])
    if 7030 in ids:
        return mm.COMIC
    b = mm.bucket_of(" ".join(getattr(release, "category_names", None) or []))
    if b:
        return b
    if ids & {7000, 7020}:
        return mm.PROSE
    return mm.UNKNOWN


def score_release(book_title: str, book_author: str | None, book_language: str | None,
                  release, prefs: dict, *, context: dict | None = None,
                  floor: float = MATCH_FLOOR, titles: list[str] | None = None,
                  want_bucket: str | None = None) -> ScoredRelease:
    raw_title = str(getattr(release, "title", "") or "")
    size = int(getattr(release, "size", 0) or 0)
    cats = getattr(release, "categories", None)
    info = parse_release(raw_title, cats)
    # Score the release against EVERY known title for the work (display + alternates), best wins — so
    # a release named with the romaji/native title still matches a work catalogued under its English
    # title. Falls back to the single title when no alternates are known.
    cand_titles = [t for t in (titles or [book_title]) if t] or [book_title]
    per_title = [(t, title_author_confidence(t, book_author, info)) for t in cand_titles]
    conf = max((c for _t, c in per_title), default=0.0)
    # Did a NON-canonical alternate title (romaji/English/native/synonym) produce a strong match? The
    # canonical/display title is cand_titles[0]; the rest are alternates. Used to relax the author-less
    # gate (a strong alt-title hit is real evidence, not a partial-title false positive).
    alt_conf = max((c for t, c in per_title if t != cand_titles[0]), default=0.0)

    reasons: list[str] = []
    accepted = True

    low = raw_title.lower()
    # Junk/obfuscated/password-spam release with no real title → hard reject.
    if info.is_junk:
        accepted = False
        reasons.append("junk/hashed release")
    if any(re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", low) for term in prefs["exclude_terms"]):
        accepted = False
        reasons.append("excluded term")   # append, don't overwrite — keep any prior reason (e.g. junk)
    # Operator required/ignored terms (Radarr-style): ignored present → reject; required absent →
    # reject. Each term is a '/regex/flags' or a case-insensitive substring.
    ignored = _compile_terms(prefs.get("ignored_terms"))
    if any(m(raw_title) for m in ignored):
        accepted = False
        reasons.append("ignored term")
    required = _compile_terms(prefs.get("required_terms"))
    if required and not any(m(raw_title) for m in required):
        accepted = False
        reasons.append("missing required term")
    # A companion product (summary / study guide / artbook) is never the work — hard reject.
    if info.is_companion:
        accepted = False
        reasons.append("companion/summary")
    # A boxset/omnibus for a SINGLE-title request is the wrong content: it already never auto-grabs,
    # but accepting it as a speculative candidate lets the cascade download it and fail verification
    # (multi-work bundle ≠ the one title), burning a download+verify cycle. Reject it outright unless
    # the operator opted into bundles (allow_boxsets). Fuzzing keeps boxsets out too — a bundle is the
    # wrong content regardless of how wide the net is cast.
    if info.is_boxset and not prefs.get("allow_boxsets"):
        accepted = False
        reasons.append("boxset/omnibus (single-title request)")
    # Title-only matches (no author to disambiguate) must be near-exact for PROSE, or they're false
    # positives that waste a grab+download — and the gate also rejects author-as-title catalog rows.
    # BUT skip it for (a) comics, where an author is STRUCTURALLY absent (comix rows carry none), so
    # the gate would reject the entire comic pipeline — precision there is already guarded by the
    # comic categories + volume gate; and (b) explicit fuzzing (floor below MATCH_FLOOR), where the
    # operator deliberately lowered the bar to cast wide and let post-download verification decide.
    # RELAX for the long tail: when the work carries ALTERNATE titles and a non-canonical alt matches
    # STRONGLY (≥ALT_TITLE_MIN_CONF, e.g. a translated/native title), that is real disambiguating
    # evidence even without an author, so the gate drops to the comic-level floor. Prose with only the
    # canonical title (or a weak alt) stays strict at NO_AUTHOR_MIN_CONF — no new false positives.
    author_less = not (book_author or "").strip()
    eff_floor = floor
    if author_less and floor >= MATCH_FLOOR and not prefs.get("is_comic"):
        strong_alt = alt_conf >= ALT_TITLE_MIN_CONF
        eff_floor = max(floor, ALT_TITLE_MIN_CONF if strong_alt else NO_AUTHOR_MIN_CONF)
    if conf < eff_floor:
        accepted = False
        reasons.append(f"low confidence {conf:.2f}")

    # Volume gate: when acquiring a KNOWN series volume (context carries its position), a release that
    # declares a DIFFERENT whole-number volume is the wrong book — reject it. This stops a volume
    # whose title is a substring of the whole series (e.g. "Spellmonger" #1) from matching
    # "Spellmonger 06 - Journeymage". Only fires when BOTH the wanted position and the release's
    # declared volume are known integers (fractional novella positions are skipped).
    want_vol = (context or {}).get("volume")
    try:
        want_vol_i = int(want_vol) if (want_vol is not None and float(want_vol).is_integer()) else None
    except (TypeError, ValueError):
        want_vol_i = None
    # The release's declared volume: parse_release catches "book/vol/#N", but these series releases
    # number bare ("Spellmonger 06"), so when we know the series name also read the number that
    # follows it in the release.
    rel_vol = info.volume
    sname = norm_title((context or {}).get("series") or "")
    if rel_vol is None and sname:
        m = re.search(re.escape(sname) + r"[\s,:#._-]*0*(\d{1,3})\b", norm_title(raw_title))
        if m:
            rel_vol = int(m.group(1))
    if want_vol_i is not None and rel_vol is not None and rel_vol != want_vol_i:
        accepted = False
        reasons.append(f"wrong volume {rel_vol} (want {want_vol_i})")

    # Format gate: audiobook vs ebook must match what the operator wants.
    if info.is_audiobook and not prefs["want_audiobooks"]:
        accepted = False
        reasons.append("audiobook not wanted")
    if not info.is_audiobook and prefs["want_ebooks"] and prefs["preferred_formats"]:
        # Unknown ebook format is allowed but penalized; a known non-preferred format is rejected —
        # UNLESS it's a Kindle format we can convert to EPUB on import (mobi/azw3) and a converter is
        # available, in which case it's acceptable.
        if info.fmt is not None and info.fmt not in prefs["preferred_formats"]:
            from . import convert
            convertible = (f".{info.fmt}" in convert.CONVERTIBLE_EXTS) and convert.available()
            if not convertible:
                accepted = False
                reasons.append(f"format {info.fmt} not preferred")

    # Language gate: if the operator restricts languages and the release declares any, require an
    # overlap (set-membership across ALL declared languages, not just the primary tag).
    declared_langs = set(info.languages) or ({info.language} if info.language else set())
    if prefs["languages"] and declared_langs and not (declared_langs & set(prefs["languages"])):
        accepted = False
        reasons.append("language " + "/".join(sorted(declared_langs)))

    size_mb = size / 1_000_000 if size else 0
    if prefs["min_size_mb"] and size_mb and size_mb < float(prefs["min_size_mb"]):
        accepted = False
        reasons.append("too small")
    if prefs["max_size_mb"] and size_mb and size_mb > float(prefs["max_size_mb"]):
        accepted = False
        reasons.append("too large")

    # Rank score: confidence dominates; small bonuses for a preferred format, retail, and grabs.
    fmt_bonus = 0.0
    if info.fmt and info.fmt in prefs["preferred_formats"]:
        idx = prefs["preferred_formats"].index(info.fmt)
        fmt_bonus = 0.15 * (len(prefs["preferred_formats"]) - idx) / len(prefs["preferred_formats"])
    retail_bonus = 0.05 if info.is_retail else 0.0
    try:
        grabs = int(getattr(release, "grabs", None) or 0)   # raw indexer field may be a string
    except (TypeError, ValueError):
        grabs = 0
    grabs_bonus = min(0.05, grabs / 2000.0)
    # Torrent health: seeders are the torrent analog of usenet grabs. A 0-seeder torrent is effectively
    # dead (it will never finish downloading), so down-rank it hard — a seeded alternative for the same
    # book always outranks it, and a 0-seeder is only ever grabbed when it's the sole candidate. A
    # healthy swarm earns a small bonus. seeders is None for usenet releases → no effect there.
    seeders = getattr(release, "seeders", None)
    try:
        seeders = int(seeders) if seeders is not None else None
    except (TypeError, ValueError):
        seeders = None
    seed_bonus = 0.0 if seeders is None else (-0.30 if seeders == 0 else min(0.05, seeders / 200.0))
    # Prefer a book's own language when known and the release declares it.
    lang_bonus = 0.05 if (book_language and book_language in
                          (info.languages or ({info.language} if info.language else set()))) else 0.0
    # Preferred terms (operator-configured) add to the rank; a proper/repack is a corrected release.
    preferred = _compile_terms(prefs.get("preferred_terms"))
    pref_bonus = min(0.2, 0.04 * sum(1 for m in preferred if m(raw_title)))
    proper_bonus = 0.03 if (info.is_proper or info.version > 1) else 0.0
    score = (conf + fmt_bonus + retail_bonus + grabs_bonus + seed_bonus
             + lang_bonus + pref_bonus + proper_bonus)
    # Type compatibility: down-rank (never reject) a release whose category type can't be the work —
    # a comic result for a prose novel, a magazine for a book. The category-scoped search already
    # filters most of this; this is the safety net for cross-posted / mis-categorised results.
    if want_bucket:
        from . import matchmeta as mm
        score *= mm.type_compat(want_bucket, _release_bucket(release))

    # --- Strict auto-grab gate (fully-automatic grabbing → false positives are the real cost) ---
    # PRECISION: the release must be essentially "title + author" with nothing unexplained. Any
    # extra meaningful token blocks auto-grab even at full title recall — a sequel subtitle ("The
    # Hero of Ages"), a bare/Roman volume number ("02", "II"), or a range ("1-4"). Computed over the
    # RAW tokens with the book's own title tokens as context, so a number that IS in the title
    # (Fahrenheit 451) is fine while a trailing volume number is not. Single letters and the pub
    # year and group tag are dropped; single DIGITS are kept (they're the dangerous volume case).
    title_toks: set[str] = set()
    for _bt in cand_titles:                       # explain tokens from ANY known title (incl. alts)
        title_toks |= set(norm_title(_bt).split())
        title_toks |= {t for t in _SPLIT_RE.split(str(_bt or "").lower()) if t}
    explained = title_toks | _author_tokens(book_author) | _STOPWORDS
    # SERIES CONTEXT (only when acquiring a known series volume): the release name legitimately
    # carries the series name, the author's full name, and the volume's position — so explain those
    # and allow a bare volume number. Blind (non-series) matching stays strict.
    ctx = context or {}
    if ctx.get("series"):
        explained |= set(norm_title(ctx["series"]).split())
    if ctx.get("author_full"):
        explained |= _author_tokens(ctx["author_full"])
    allow_vol = bool(ctx.get("allow_volume"))

    def _meta(t: str) -> bool:
        return (
            t in _NOISE_TOKENS or t in _LANG_TOKENS or t in _AUDIO_FORMATS or t in _EBOOK_SET
            or t in _EDITION_TOKENS or t in _BOXSET_TOKENS or t in _COMPANION_TOKENS
            or t == info.group or (len(t) <= 1 and not t.isdigit())
            or (allow_vol and t.isdigit())              # expected volume number in a series grab
            or (_YEAR_RE.match(t) and t not in title_toks)
        )
    unexplained = {t for t in info.raw_tokens if t and not _meta(t) and t not in explained}
    # LANGUAGE (auto): match the BOOK's own language against what the release declares. If the
    # release declares languages, the book's language must be among them; if it declares none, a
    # non-English book is unsafe (untagged foreign releases are common) while English is assumed.
    if book_language:
        if declared_langs:
            lang_safe = book_language in declared_langs
        else:
            lang_safe = book_language == "en"
    else:
        lang_safe = True

    base_ok = (
        accepted
        and conf >= prefs["auto_grab_min_confidence"]
        and (info.is_audiobook or info.fmt is not None)  # never auto-grab an unknown-format blob
        and bool(getattr(release, "download_url", None))
        and not info.is_boxset                           # don't grab an omnibus/boxset for a single
        and lang_safe
    )
    series_in_release = False
    if ctx.get("series"):
        st = set(norm_title(ctx["series"]).split())
        series_in_release = bool(st) and st <= set(info.content_tokens)
    if ctx.get("series"):
        # Known series volume: the volume TITLE (high recall, in conf) + author surname + the series
        # name appearing in the release identify it; the position/first-name/series tokens are
        # expected, so we don't require zero-unexplained or volume==1.
        auto_ok = base_ok and series_in_release
    else:
        # The parsed volume only disqualifies a non-series grab when the title doesn't ACCOUNT for that
        # number — a standalone whose real title is e.g. "Volume 2"/"Part Two" parses info.volume=2 but
        # that digit is in the title tokens, so it shouldn't be treated as a stray series volume.
        vol_in_title = info.volume is not None and str(info.volume) in title_toks
        auto_ok = base_ok and (info.volume in (None, 1) or vol_in_title) and not unexplained
    if accepted and not auto_ok:
        why = []
        if info.is_boxset:
            why.append("boxset")
        if not ctx.get("series") and info.volume not in (None, 1) and not (
                info.volume is not None and str(info.volume) in title_toks):
            why.append(f"vol {info.volume}")
        if not ctx.get("series") and unexplained:
            why.append("extra tokens")
        if ctx.get("series") and not series_in_release:
            why.append("series name absent")
        if not lang_safe:
            why.append("lang unconfirmed")
        if why:
            reasons.append("not auto: " + "+".join(why))
    reason = ", ".join(reasons) if reasons else (
        f"{info.fmt or 'unknown'} · conf {conf:.2f}"
    )
    return ScoredRelease(
        release=release, info=info, confidence=conf, score=round(score, 4),
        accepted=accepted, auto_ok=auto_ok, reason=reason,
    )


def _dedup_key(sr: ScoredRelease) -> tuple:
    # Collapse duplicate listings of the same content+format (indexers cross-post heavily). Use the
    # ORDERED token sequence so anagram titles ("Way of Kings" vs "Kings of Way") don't collapse.
    return (sr.info.content_seq, sr.info.fmt or "?", sr.info.volume, sr.info.is_boxset)


def rank_releases(book_title: str, book_author: str | None, book_language: str | None,
                  releases: list, prefs: dict, *, context: dict | None = None,
                  floor: float = MATCH_FLOOR, titles: list[str] | None = None,
                  want_bucket: str | None = None) -> list[ScoredRelease]:
    """Score, dedup, and rank candidate releases (accepted ones, best first). One malformed
    release never aborts the batch. ``floor`` lowers the accept bar (book-fuzzing: try the long
    tail and let post-download verification decide). ``titles`` adds the work's alternate titles to
    the match, ``want_bucket`` its type (prose/comic) for cross-type down-ranking."""
    best: dict[tuple, ScoredRelease] = {}
    for r in releases:
        try:
            sr = score_release(book_title, book_author, book_language, r, prefs,
                               context=context, floor=floor, titles=titles, want_bucket=want_bucket)
        except Exception:  # noqa: BLE001
            log.info("scoring release failed: %r", getattr(r, "title", r))
            continue
        if not sr.accepted:
            continue
        k = _dedup_key(sr)
        cur = best.get(k)
        if cur is None or sr.score > cur.score:
            best[k] = sr
    return sorted(best.values(), key=lambda s: s.score, reverse=True)


def _first_author(author: str | None) -> str:
    """The first listed author (drop co-authors), in 'First Last' order, lowercased."""
    if not author:
        return ""
    if "," in author:
        parts = [p.strip() for p in author.split(",")]
        # "Last, First" → "First Last"; a comma-list of authors → just the first name.
        if len(parts) >= 2 and parts[1] and " " not in parts[0]:
            return f"{parts[1]} {parts[0]}".lower().strip()
        return parts[0].lower().strip()
    return re.split(r"[&;]| and ", author)[0].strip().lower()


def _surname(author: str | None) -> str:
    full = _first_author(author)
    return full.split()[-1] if full.split() else ""


def _strip_subtitle(title: str) -> str:
    """The main title before a ': subtitle' or ' - subtitle' tail (kept if substantial)."""
    t = (title or "").strip()
    for sep in (": ", " - ", " – ", " — "):
        if sep in t:
            head = t.split(sep, 1)[0].strip()
            if len(head) >= 3:
                return head
    return t


def build_query(title: str, author: str | None) -> str:
    """A clean Prowlarr query: normalized title plus the author's distinctive surname token."""
    t = norm_title(title)
    auth = _surname(author)
    return (f"{t} {auth}").strip() if auth and auth not in t else t


def query_variants(title: str, author: str | None, *, context: dict | None = None,
                   isbns: list | None = None, alt_titles: list[str] | None = None) -> list[str]:
    """Several distinct Prowlarr queries for one book, using different information / naming
    conventions — so a release the canonical query misses (different author rendering, a dropped
    subtitle, a series+volume name, an ISBN, or an ALTERNATE title like a manga's romaji name) is
    still found. Order = most-to-least specific; the caller searches all and merges. De-duplicated,
    case-insensitively."""
    ctx = context or {}
    out: list[str] = []
    seen: set[str] = set()

    def add(q: str | None) -> None:
        q = (q or "").strip()
        if q and q.lower() not in seen:
            seen.add(q.lower())
            out.append(q)

    nt = norm_title(title)
    full = _first_author(author)
    add(build_query(title, author))            # title + surname (canonical)
    if full:
        add(f"{nt} {full}")                    # title + full first-author name
    add(nt)                                     # title only (author rendering varies wildly)
    base = _strip_subtitle(title)              # subtitle-stripped variants
    if norm_title(base) != nt:
        add(build_query(base, author))
        add(norm_title(base))
    for alt in (alt_titles or []):             # alternate titles (romaji/english/native/synonyms)
        ant = norm_title(alt)
        if ant and ant != nt:
            add(build_query(alt, author))
            add(ant)
    series = ctx.get("series")                 # series + volume (for known series volumes)
    if series:
        sv = norm_title(series)
        vol = ctx.get("volume")
        if isinstance(vol, int) and vol > 0:
            add(f"{sv} {vol:02d}")
            add(f"{sv} {vol}")
        add(f"{sv} {nt}")
    for isbn in (isbns or [])[:2]:             # ISBN (exact, when the indexer supports it)
        digits = re.sub(r"[^0-9Xx]", "", str(isbn))
        if len(digits) in (10, 13):
            add(digits)
    return out


def candidate_dicts(ranked: list[ScoredRelease], *, cap: int = 6,
                    include_speculative: bool = True) -> list[dict]:
    """Flatten ranked releases into serializable candidate descriptors for a download cascade.
    Auto-grabbable releases come first (high confidence, try them outright); accepted-but-sub-auto
    releases follow as SPECULATIVE candidates (download + post-download verify decides them). Only
    releases with a usable download URL are included."""
    auto: list[dict] = []
    spec: list[dict] = []
    for sr in ranked:
        r = sr.release
        url = getattr(r, "download_url", None)
        if not url:
            continue
        cand = {
            "title": getattr(r, "title", None),
            "download_url": url,
            "guid": getattr(r, "guid", None),
            "indexer": getattr(r, "indexer", None),
            "size": int(getattr(r, "size", 0) or 0),
            "fmt": sr.info.fmt,
            "confidence": round(sr.confidence, 4),
            "auto_ok": sr.auto_ok,
            "is_multi": sr.info.is_boxset,        # a pack/omnibus → may contain several books
            "key": release_key(r),
        }
        (auto if sr.auto_ok else spec).append(cand)
    if not include_speculative:
        spec = []
    return (auto + spec)[:cap]


FUZZ_FLOOR = 0.3  # book-fuzzing: try low-confidence releases too; post-download verify decides


async def find_releases(db: Session, book: CatalogWork, *, limit: int = 100,
                        context: dict | None = None, fuzz: bool = False,
                        protocols: tuple[str, ...] | None = None) -> list[ScoredRelease]:
    """Search the configured Prowlarr for releases of `book` and return ranked candidates.

    Runs several query variants (different naming conventions) concurrently and merges them, drops
    releases already recorded as broken (dead/wrong links never retried), then scores+ranks the
    union. Returns [] (not an error) when no Prowlarr is configured. ``context`` (series name + full
    author + volume) widens the queries and relaxes the precision gate for a known series volume.
    ``fuzz`` lowers the accept floor so even low-confidence releases are returned (the cascade
    downloads + content-verifies each — used by the 'find anyway' book-fuzzing job)."""
    from ..integrations.prowlarr import ProwlarrClient

    integ = get_prowlarr(db)
    if integ is None:
        return []
    # A comic/manga catalog work searches comic categories (7030) for CBZ/CBR; prose searches ebooks.
    prefs = search_prefs(integ, media_kind=(book.media_kind or "text"))
    if protocols is not None:   # torrent route forces ("torrent",); usenet pipeline uses the config default
        prefs = {**prefs, "protocols": tuple(protocols)}
    client = ProwlarrClient(integ.base_url, integ.api_key)
    isbns = (book.extra or {}).get("isbn") if isinstance(getattr(book, "extra", None), dict) else None
    # Pull the work's persisted/just-fetched match metadata: alternate titles widen the search, and
    # the type bucket (prose/comic) lets scoring down-rank a cross-typed release.
    from . import matchmeta
    meta = await matchmeta.get_work_meta(db, book)
    alt_titles = meta.titles[1:] if len(meta.titles) > 1 else None
    variants = query_variants(book.title, book.author, context=context, isbns=isbns,
                              alt_titles=alt_titles)
    # Structured book-search pass (13A): an ISBN and the canonical title+author are sent as a
    # type=book query so book-capable indexers match them as real metadata (retail releases keyed by
    # ISBN, scoped to book categories) instead of an ignored free-text digit string. Additive — the
    # results merge into and de-dupe against the free-text variants below.
    book_queries: list[str] = []
    seen_bq: set[str] = set()

    def _add_bq(q: str | None) -> None:
        q = (q or "").strip()
        if q and q.lower() not in seen_bq:
            seen_bq.add(q.lower())
            book_queries.append(q)

    for isbn in (isbns or [])[:2]:
        digits = re.sub(r"[^0-9Xx]", "", str(isbn))
        if len(digits) in (10, 13):
            _add_bq(digits)
    _add_bq(build_query(book.title, book.author))

    async def _one(q: str, search_type: str = "search"):
        try:
            return await client.search(
                q, categories=prefs["categories"], indexer_ids=prefs["indexer_ids"],
                protocols=prefs["protocols"], limit=limit, search_type=search_type,
            )
        except Exception as exc:  # noqa: BLE001 — one flaky variant must not abort the whole search
            log.info("prowlarr %s search failed for %r: %s", search_type, q, exc)
            return []

    tasks = [_one(q) for q in variants] + [_one(q, "book") for q in book_queries]
    batches = await asyncio.gather(*tasks, return_exceptions=True)
    bad = broken_keys(db)
    merged: dict[str, object] = {}
    for batch in batches:
        if not isinstance(batch, list):  # a gather slot that raised despite _one's guard
            continue
        for r in batch:
            k = release_key(r) or f"t:{getattr(r, 'title', '') or ''}"
            if k in bad:                          # known dead/wrong link → never offer it again
                continue
            merged.setdefault(k, r)
    return rank_releases(book.title, book.author, book.language, list(merged.values()),
                         prefs, context=context, floor=(FUZZ_FLOOR if fuzz else MATCH_FLOOR),
                         titles=meta.titles, want_bucket=meta.bucket)
