"""Shared work→search metadata for the acquisition matchers (libgen + usenet).

Both pipelines should search with EVERY known title for a work (its romaji/english/native/synonyms,
not just the catalog display title) and should reject hits whose TYPE can't be the work — a journal
article or a comic when we want a prose novel, or a prose book when we want a comic. This module is
the single source of that metadata:

  * It reads what enrichment has already persisted on ``CatalogWork.extra``.
  * On a miss it fetches the missing bits ONCE from the metadata API (AniList for comics, Open
    Library for prose) and writes them back to ``extra`` — so the same call is never paid twice
    (per the "fetch once, store permanently" rule).
  * When nothing extra is available it degrades to plain title/author matching.

Type matching is "penalize, never drop": a mismatched type is heavily down-ranked (so it only wins
if there is genuinely nothing better) rather than removed outright.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx

from ..models import CatalogWork
from .extract import norm_title

log = logging.getLogger("shelf.matchmeta")

_ANILIST_API = "https://graphql.anilist.co"
_OPENLIBRARY = "https://openlibrary.org/search.json"
_UA = "Mozilla/5.0 (compatible; ShelfReader/0.1)"

# Coarse type buckets used for cross-provider compatibility. Anything we can't classify is UNKNOWN,
# which never penalises (fallback to title/author only).
PROSE = "prose"        # novel / light novel / non-fiction / general book
COMIC = "comic"        # comic / manga / manhwa / manhua / graphic novel
ARTICLE = "article"    # journal / magazine / paper / periodical — almost never a requested "book"
UNKNOWN = ""

# Match the libgen/anna type badges ("Comics issue", "Journal article", "Magazine", "Book") and
# similar provider labels — plurals included (a bare \bcomic\b would miss "Comics issue").
_COMIC_WORDS = re.compile(r"\b(comics?|mangas?|manhwa|manhua|graphic\s*novels?|webtoons?|cbz|cbr)\b", re.I)
_ARTICLE_WORDS = re.compile(r"\b(articles?|journals?|magazines?|periodicals?|papers?|proceedings)\b", re.I)
_PROSE_WORDS = re.compile(r"\b(books?|novels?|fiction|non[- ]?fiction|light\s*novels?|textbooks?|story)\b", re.I)


def bucket_of(raw_type: str | None, *, media_kind: str | None = None) -> str:
    """Map a provider's type string (libgen "Book"/"Comic"/"Article" badge, a newznab category name,
    an AniList format, a comix_type) — or a Shelf ``media_kind`` — to a coarse bucket. Order matters:
    article/comic are checked before the broad prose words so "Comic Book" → comic, not prose."""
    s = (raw_type or "").strip().lower()
    if s:
        if _ARTICLE_WORDS.search(s):
            return ARTICLE
        if _COMIC_WORDS.search(s):
            return COMIC
        if s in {"novel", "light_novel", "lightnovel", "ln"} or _PROSE_WORDS.search(s):
            return PROSE
    mk = (media_kind or "").strip().lower()
    if mk == "comic":
        return COMIC
    if mk == "text":
        return PROSE
    return UNKNOWN


def type_compat(want_bucket: str, hit_bucket: str) -> float:
    """Compatibility multiplier between the wanted work's bucket and a hit's bucket. 1.0 = compatible
    or unknown (no penalty — fallback); <1.0 = a type mismatch that should sink the hit but not drop
    it. An article is almost never the requested book/comic, so it is penalised hardest."""
    if not want_bucket or not hit_bucket:
        return 1.0                      # unknown on either side → don't penalise (title/author only)
    if want_bucket == hit_bucket:
        return 1.0
    if hit_bucket == ARTICLE or want_bucket == ARTICLE:
        return 0.25                     # journal/magazine vs a real book → almost certainly wrong
    if {want_bucket, hit_bucket} == {PROSE, COMIC}:
        return 0.4                      # prose vs comic → wrong medium
    return 0.6


# A bracketed/parenthetical qualifier ("(Illustrated)", "[Unabridged]") and an edition tail are noise
# for a search term — strip them so a clean title query matches the most editions.
_PAREN = re.compile(r"\s*[\(\[][^)\]]*[\)\]]")
_EDITION_TAIL = re.compile(r"\s*[:\-–—]\s*(the\s+)?(complete|definitive|annotated|illustrated|revised|"
                           r"unabridged|special|collector'?s|anniversary|deluxe|expanded)\b.*$", re.I)


def clean_search_title(t: str | None) -> str:
    t = _PAREN.sub("", t or "")
    t = _EDITION_TAIL.sub("", t)
    return " ".join(t.split()).strip()


@dataclass
class WorkMeta:
    """Everything the matchers need about a work, with API data already merged in."""
    titles: list[str]                       # every known title (display first, then alts), cleaned
    author: str | None
    language: str | None
    bucket: str                             # PROSE | COMIC | ARTICLE | UNKNOWN (the wanted type)
    media_kind: str
    raw: dict = field(default_factory=dict)  # the persisted match-meta (for debugging)

    @property
    def primary_title(self) -> str:
        return self.titles[0] if self.titles else ""


def _dedup_titles(titles: list[str]) -> list[str]:
    out, seen = [], set()
    for t in titles:
        ct = clean_search_title(t)
        key = norm_title(ct)
        if ct and key and key not in seen:
            seen.add(key)
            out.append(ct)
    return out


def title_variants(meta: WorkMeta, *, cap: int = 4) -> list[str]:
    """The distinct title strings to actually SEARCH with — the work's known titles (display +
    alternates), most-canonical first, capped so we don't fan out into too many provider calls."""
    return _dedup_titles(meta.titles)[:cap]


# ----------------------------------------------------------------- one-time API fetch + persist
async def _fetch_anilist(title: str) -> tuple[list[str], str]:
    """AniList romaji/english/native + synonyms for a comic/manga title (its alt titles are the big
    win — "Shingeki no Kyojin" vs "Attack on Titan"). Returns (alt_titles, content_type).

    Picks the BEST + STRONG match, not merely the first ≥0.5-overlap hit: a common-worded title
    ("Kingdom", "Berserk") otherwise permanently cached the WRONG series' romaji/native names (the
    match_meta_at marker stops refetch), poisoning all future matching. So each candidate must clear
    a high char-level ratio (≥90) against one of its names, and among those that do we keep the
    most-POPULAR (popularity breaks the common-title tie toward the canonical work). (13A)"""
    from .fuzzy import token_sort_ratio
    q = ("query($q:String){Page(perPage:8){media(search:$q,type:MANGA,sort:SEARCH_MATCH){"
         "format popularity synonyms title{romaji english native}}}}")
    async with httpx.AsyncClient(timeout=15.0, headers={"User-Agent": _UA}) as client:
        r = await client.post(_ANILIST_API, json={"query": q, "variables": {"q": title}})
    if r.status_code != 200:
        raise httpx.HTTPError(f"anilist HTTP {r.status_code}")
    media = (((r.json() or {}).get("data") or {}).get("Page") or {}).get("media") or []
    want_norm = norm_title(title)
    want = set(want_norm.split())
    best = None
    best_key = (-1.0, -1)  # (ratio, popularity)
    for m in media:
        t = m.get("title") or {}
        names = [t.get("romaji"), t.get("english"), t.get("native"), *(m.get("synonyms") or [])]
        toks = set(norm_title(" ".join(n for n in names if n)).split())
        if not (want and toks and len(want & toks) / len(want) >= 0.5):
            continue
        # Best char-level ratio of the query against ANY of this candidate's names.
        ratio = max((token_sort_ratio(want_norm, norm_title(n)) for n in names if n), default=0.0)
        if ratio < 90:
            continue                                   # too weak → don't risk a poisoning false match
        key = (ratio, int(m.get("popularity") or 0))
        if key > best_key:
            best, best_key = m, key
    if best is None:
        return [], COMIC          # no confident match, but it IS a comic search → record the type
    t = best.get("title") or {}
    alts = [t.get("romaji"), t.get("english"), t.get("native"), *(best.get("synonyms") or [])]
    fmt = (best.get("format") or "").upper()
    ctype = PROSE if fmt == "NOVEL" else COMIC
    return [a for a in alts if a], ctype


async def _fetch_openlibrary(title: str, author: str | None) -> tuple[list[str], str]:
    """Open Library alternative titles for a prose book. OL's coverage of alt titles is thin, so the
    main value here is recording the type (a book) — which lets us penalise article/comic hits."""
    params = {"title": title, "fields": "title,alternative_title,subtitle", "limit": "3"}
    if author:
        params["author"] = author.split(",")[0]
    async with httpx.AsyncClient(timeout=15.0, headers={"User-Agent": _UA}, follow_redirects=True) as client:
        r = await client.get(_OPENLIBRARY, params=params)
    if r.status_code != 200:
        raise httpx.HTTPError(f"openlibrary HTTP {r.status_code}")
    docs = ((r.json() or {}).get("docs") or [])
    want = set(norm_title(title).split())
    alts: list[str] = []
    for d in docs:
        toks = set(norm_title(d.get("title") or "").split())
        if want and toks and len(want & toks) / len(want) >= 0.6:
            alts.extend([d.get("title"), *(d.get("alternative_title") or [])])
            break
    return [a for a in alts if a], PROSE


async def _fetch_match_meta(cw: CatalogWork) -> tuple[list[str], str]:
    """One-time metadata fetch for a work that enrichment hasn't covered yet. Comics → AniList,
    prose → Open Library. Best-effort: returns ([], type-from-media_kind) on no match."""
    is_comic = (cw.media_kind or "").lower() == "comic"
    title = clean_search_title(cw.title)
    if not title:
        return [], bucket_of(None, media_kind=cw.media_kind)
    if is_comic:
        return await _fetch_anilist(title)
    return await _fetch_openlibrary(title, cw.author)


def _persist(db, cw: CatalogWork, alt_titles: list[str], content_type: str) -> None:
    """Write the fetched match-meta onto extra (+ a timestamp marker so we never refetch it)."""
    extra = dict(cw.extra or {})
    # Keep alt titles that add something beyond the display title.
    base = norm_title(cw.title or "")
    extra["alt_titles"] = _dedup_titles([cw.title, *alt_titles])[1:] if alt_titles else \
        [t for t in extra.get("alt_titles", []) if norm_title(t) != base]
    if content_type and not extra.get("content_type"):
        extra["content_type"] = content_type      # don't clobber a richer type set by enrichment
    extra["match_meta_at"] = datetime.now(UTC).isoformat()
    cw.extra = extra
    try:
        db.commit()
    except Exception:  # noqa: BLE001 — persistence is best-effort; matching still works in-memory
        db.rollback()
        log.exception("match-meta persist failed for catalog_work %s", getattr(cw, "id", "?"))


async def get_work_meta(db, cw: CatalogWork, *, allow_fetch: bool = True) -> WorkMeta:
    """The work's matching metadata: persisted alt-titles + content type, fetched once on a miss.

    ``allow_fetch=False`` reads only what's already stored (no API calls) — used where a search-time
    network call isn't wanted.
    """
    extra = dict(cw.extra or {})
    alts = list(extra.get("alt_titles") or [])
    ctype = extra.get("content_type") or ""
    fetched = bool(extra.get("match_meta_at"))
    # Fetch alt-titles once if we've never run AND none are stored — a content_type written by
    # enrichment must NOT suppress the alt-title fetch (only matchmeta's own marker does).
    if allow_fetch and not fetched and not alts:
        try:
            got_alts, got_type = await _fetch_match_meta(cw)
            _persist(db, cw, got_alts, got_type)
            extra = dict(cw.extra or {})
            alts = list(extra.get("alt_titles") or [])
            ctype = extra.get("content_type") or ""
        except (httpx.HTTPError, ValueError) as exc:
            # Transient upstream failure — leave it unmarked so a later search can retry the fetch.
            log.info("match-meta fetch skipped for %r: %s", (cw.title or "")[:40], exc)
    titles = _dedup_titles([cw.title, *alts]) or [cw.title or ""]
    bucket = bucket_of(ctype, media_kind=cw.media_kind)
    return WorkMeta(titles=titles, author=cw.author, language=cw.language,
                    bucket=bucket, media_kind=cw.media_kind or "", raw=extra)
