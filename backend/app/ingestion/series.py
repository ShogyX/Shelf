"""Series detection + selective acquisition.

Many books belong to a series/trilogy. Given a catalog book, detect its series and enumerate the
sibling volumes (ordered), so the UI can offer "fetch the whole series" or a custom selection. The
chosen volumes are acquired through the normal route priority (web hook / manager / usenet pipeline).

Series data comes from Open Library's ``series`` field (keyless, broad). When a book has no series we
return nothing — callers show a graceful "no series found".
"""
from __future__ import annotations

import logging
import re

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import CatalogWork
from .book_catalog import OPENLIBRARY, _UA, _ol_cover
from .extract import authors_compatible, norm_title

log = logging.getLogger("shelf.series")

_OL_FIELDS = "key,title,author_name,first_publish_year,cover_i,series,readinglog_count"
_TIMEOUT = 20.0
SERIES_ACQUIRE_CAP = 30   # max volumes acquired in one request (bounds latency + grabs)
# A trailing volume number on a series label: "Mistborn (1)" / "Discworld #8" / "Wheel of Time, 4".
_SERIES_NUM_RE = re.compile(r"[\s,#:(\[]+(\d{1,3})\s*[)\]]?\s*$")


def parse_series_label(raw: str | None) -> tuple[str | None, int | None]:
    """Split a raw OL series label into (name, position)."""
    if not raw:
        return (None, None)
    s = str(raw).strip()
    m = _SERIES_NUM_RE.search(s)
    pos = int(m.group(1)) if m else None
    name = (_SERIES_NUM_RE.sub("", s).strip(" -–—,:#([") if m else s).strip()
    return (name or None, pos)


async def _ol_query(client: httpx.AsyncClient, q: str, *, limit: int) -> list[dict]:
    from urllib.parse import quote_plus
    url = f"{OPENLIBRARY}/search.json?q={quote_plus(q)}&fields={_OL_FIELDS}&limit={limit}"
    try:
        r = await client.get(url, headers={"Accept": "application/json", "User-Agent": _UA})
    except httpx.HTTPError as exc:
        log.info("series OL query failed: %s", exc)
        return []
    if r.status_code != 200:
        return []
    return (r.json() or {}).get("docs", []) or []


def _series_from_title(title: str) -> str | None:
    """Best-effort series name from a title's shape: 'Series: Subtitle' or 'Title (Series, #N)'.
    (Open Library's series field is usually null, so this catches the common formatting.)"""
    t = (title or "").strip()
    m = re.search(r"\(([^)]+?)(?:[\s,#]+\d{1,3})?\)\s*$", t)  # 'Title (Series #2)'
    if m:
        cand = m.group(1).strip(" ,#")
        if len(cand) >= 3 and not cand.isdigit():
            return cand
    if ":" in t:  # 'Series: Subtitle'
        head = t.split(":", 1)[0].strip()
        if len(head.split()) <= 5 and len(head) >= 3:
            return head
    return None


def _valid_series_name(name: str | None) -> str | None:
    """Reject boxset/omnibus 'names' (a comma-list of every volume) and other junk."""
    if not name:
        return None
    n = name.strip()
    if len(n) > 45 or "," in n or "&" in n or "/" in n:
        return None
    return n or None


async def _series_name_for(client: httpx.AsyncClient, cw: CatalogWork) -> str | None:
    """The book's series name: stored extra → title shape → a live OL lookup."""
    name = _valid_series_name(parse_series_label((cw.extra or {}).get("series"))[0])
    if name:
        return name
    name = _valid_series_name(_series_from_title(cw.title))
    if name:
        return name
    docs = await _ol_query(client, f"{cw.title} {cw.author or ''}".strip(), limit=5)
    tq = set(norm_title(cw.title).split())
    for d in docs:
        if not d.get("series"):
            continue
        dt = set(norm_title(d.get("title") or "").split())
        if tq and dt and len(tq & dt) / len(tq | dt) >= 0.6:
            nm = _valid_series_name(parse_series_label((d.get("series") or [None])[0])[0])
            if nm:
                return nm
    return None


# Phrase-based so we drop real bundles/omnibus without nuking legit volumes whose title merely
# contains a word like "Game" (e.g. "A Game of Thrones") or "Complete".
_BUNDLE_RE = re.compile(
    r"\b(saga|omnibus|box ?set|boxed set|sampler|companion|anthology|tetralogy|coffret|"
    r"complete (series|collection|saga)|\d+[\s-]*book(s)?|book\s*\d+\s*[-–]\s*\d+)\b"
    r"|\bcollection\b|\bcollected\b|\btrilogy\b|\[\d+\s*/\s*\d+\]", re.I,
)


async def detect_series(db: Session, cw: CatalogWork) -> dict:
    """Detect `cw`'s series and enumerate its volumes (ordered). Returns {series, books:[...]}.
    Each book: title, author, year, position, cover_url, ref (OL key), catalog_id, hooked_work_id.

    Open Library's series field is sparse, so membership is established two ways: the OL
    ``series:"<name>"`` filter (authoritative), and author+title-contains (for series whose volume
    titles share the name, e.g. Dune / Harry Potter). Bundles/omnibus and wrong-author hits are
    dropped. Coverage is best with a Google Books API key configured."""
    async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
        name = await _series_name_for(client, cw)
        if not name:
            return {"series": None, "books": []}
        want = norm_title(name)
        wset = set(want.split())
        by_filter = await _ol_query(client, f'series:"{name}"', limit=40)
        by_author = await _ol_query(client, f"{cw.author or ''} {name}".strip(), limit=40)

    found: dict[str, dict] = {}

    def _consider(d: dict, trusted: bool) -> None:
        title = (d.get("title") or "").strip()
        if not title or _BUNDLE_RE.search(title):
            return
        nk = norm_title(title)
        if not nk or nk in found:
            return
        sname, pos = parse_series_label((d.get("series") or [None])[0])
        ntoks = set(nk.split())
        is_member = trusted or (sname and norm_title(sname) == want) or (wset and wset <= ntoks)
        if not is_member:
            return
        authors = ", ".join(d.get("author_name") or []) or None
        if cw.author and not authors_compatible(cw.author, authors):
            return
        found[nk] = {
            "title": title, "author": authors, "year": d.get("first_publish_year"),
            "position": pos, "cover_url": _ol_cover(d.get("cover_i")),
            "ref": d.get("key"), "norm_key": nk,
        }

    for d in by_filter:      # OL series: filter asserts membership
        _consider(d, True)
    for d in by_author:      # require series-match or title-contains
        _consider(d, False)

    books = sorted(found.values(), key=lambda b: (b["position"] or 999, b["year"] or 9999, b["title"]))
    # Annotate library/catalog status from what we already have.
    for b in books:
        existing = db.scalar(
            select(CatalogWork).where(CatalogWork.norm_key == b["norm_key"]).limit(1)
        )
        b["catalog_id"] = existing.id if existing else None
        b["hooked_work_id"] = existing.hooked_work_id if existing else None
        b.pop("norm_key", None)
    return {"series": name, "books": books}


def _pick_by_author(db: Session, nk: str, author: str | None) -> CatalogWork | None:
    """An unhooked catalog row for `nk` whose author matches — so a same-title wrong-author edition
    (e.g. a study guide) can't be grabbed as the series volume."""
    rows = db.scalars(
        select(CatalogWork).where(CatalogWork.norm_key == nk, CatalogWork.hooked_work_id.is_(None))
    ).all()
    if not rows:
        return None
    if author:
        for r in rows:
            if authors_compatible(author, r.author):
                return r
        return None  # rows exist but none match the author → resolve fresh
    return rows[0]


async def _resolve_book_row(db: Session, title: str, author: str | None) -> CatalogWork | None:
    """Find (or live-resolve) a not-yet-hooked, author-matching catalog row for a series volume."""
    from . import book_catalog
    nk = norm_title(title)
    row = _pick_by_author(db, nk, author)
    if row is not None:
        return row
    try:
        await book_catalog.resolve_live(db, f"{title} {author or ''}".strip())
    except Exception:  # noqa: BLE001
        return None
    return _pick_by_author(db, nk, author)


async def acquire_series(db: Session, cw: CatalogWork, *, refs: list[str] | None, want_all: bool,
                         user_id: int, shelf_id: int | None = None) -> list[dict]:
    """Acquire selected series volumes (by OL ref, or all) via the user's route priority."""
    from . import acquire as acq
    detected = await detect_series(db, cw)
    chosen = [b for b in detected["books"] if want_all or (refs and b["ref"] in refs)]
    # Bound the synchronous work so a huge series can't time out the request or flood the grabber.
    capped = chosen[:SERIES_ACQUIRE_CAP]
    priority = acq.user_priority(db, _user(db, user_id))
    results: list[dict] = []
    if len(chosen) > len(capped):
        log.info("series acquire capped at %s of %s volumes", len(capped), len(chosen))
    chosen = capped
    for b in chosen:
        if b.get("hooked_work_id"):
            results.append({"title": b["title"], "ref": b["ref"], "status": "in_library"})
            continue
        row = await _resolve_book_row(db, b["title"], b["author"])
        if row is None:
            results.append({"title": b["title"], "ref": b["ref"], "status": "unresolved"})
            continue
        try:
            res = await acq.acquire(db, row, user_id=user_id, priority=priority, shelf_id=shelf_id)
        except Exception as exc:  # noqa: BLE001
            results.append({"title": b["title"], "ref": b["ref"], "status": "error", "detail": str(exc)})
            continue
        results.append({"title": b["title"], "ref": b["ref"], **res})
    return results


def _user(db: Session, user_id: int):
    from ..models import User
    return db.get(User, user_id)
