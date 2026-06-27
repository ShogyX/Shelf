"""Import + monitor external reading lists / libraries.

``fetch_list(provider, list_ref, ...)`` reads a user's list from one of the supported sites and returns
its titles as ``ListItem`` rows (title + author + media hint). The preview/confirm API matches each item
to the local catalog; ``list_sync_tick`` re-fetches periodically and auto-acquires NEW titles.

Providers (all read-only, mostly no-auth):
  * anilist        — GraphQL MediaListCollection(userName) — manga + light novels
  * goodreads      — public shelf RSS feed (the API is gone)
  * openlibrary    — the user's public reading-log JSON
  * hardcover      — GraphQL user_books (uses the configured Hardcover token)
  * mal            — MyAnimeList via the public Jikan API
  * amazon_wishlist— scrape a PUBLIC Amazon wishlist page (fragile; user keeps it public)

Each fetcher is best-effort: a transient/format error raises ``ListImportError`` so the caller can show
it (preview) or record last_error (tick) without crashing.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import feedparser
import httpx

from .. import telemetry
from ..models import _utcnow
from . import netguard

log = logging.getLogger("shelf.list_import")

_TIMEOUT = 20.0
_UA = "Shelf/1.0 (reading-list import)"
# Global safety caps so a pathological list (e.g. a 76k-title shelf) can't run forever. Hitting either
# truncates the import and logs a warning (never silent) — providers paginate up to these bounds.
MAX_LIST_ITEMS = 20000
MAX_PAGES = 300
# A real browser UA for Amazon, which serves a blocked/JS page to obvious bots.
_BROWSER_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
               "Chrome/124.0 Safari/537.36")

PROVIDERS = ("anilist", "goodreads", "openlibrary", "hardcover", "mal", "amazon_wishlist")

# Human labels + which sub-list a provider exposes (for the UI dropdown / list_name).
PROVIDER_LISTS: dict[str, list[str]] = {
    "anilist": ["PLANNING", "CURRENT", "COMPLETED", "PAUSED", "REPEATING"],
    "goodreads": ["to-read", "currently-reading", "read"],
    "openlibrary": ["want-to-read", "currently-reading", "already-read"],
    "hardcover": ["want", "reading", "read"],
    "mal": ["plan_to_read", "reading", "completed"],
    "amazon_wishlist": [],
}


class ListImportError(Exception):
    """A list couldn't be read (bad username/URL, private list, provider down, parse failure)."""


@dataclass
class ListItem:
    title: str
    author: str | None = None
    # text | comic (manga). Only AniList/MAL report it; the rest (Goodreads/OpenLibrary/Hardcover/
    # Amazon) default to "text" — a manga from those won't match a comics-only crawl source and instead
    # falls through to the type-ranking download routes, which is the accepted trade-off for strictness.
    media_kind: str = "text"
    ext_id: str | None = None
    cover_url: str | None = None


async def fetch_list(provider: str, list_ref: str, *, list_name: str | None = None,
                     config: dict | None = None, limit: int | None = None) -> list[ListItem]:
    """Read an external list. Returns de-duplicated ListItems (by normalized title+author). ``limit``
    stops early with a bounded SAMPLE (used by the preview so a 76k Listopia list isn't fully scraped
    just to show the add dialog — the full fetch happens later in the background)."""
    config = config or {}
    fn = _FETCHERS.get(provider)
    if fn is None:
        raise ListImportError(f"unknown list provider: {provider!r}")
    if not (list_ref or "").strip():
        raise ListImportError("a username or list URL is required")
    try:
        if provider == "goodreads":
            # Goodreads carries the huge Listopia case → it honours `limit` natively (stops early).
            items = await _goodreads(list_ref.strip(), list_name, config, limit=limit)
        else:
            items = await fn(list_ref.strip(), list_name, config)
            if limit:
                items = items[:limit]   # API-paginated + capped providers; bound the sample post-hoc
    except netguard.BlockedAddress as exc:
        raise ListImportError("that URL points at a blocked (internal) address") from exc
    # De-dup: an external list can repeat a title across volumes/editions.
    seen: set[tuple[str, str]] = set()
    out: list[ListItem] = []
    for it in items:
        if not it.title:
            continue
        k = (re.sub(r"\W+", "", it.title.lower()), re.sub(r"\W+", "", (it.author or "").lower()))
        if k in seen:
            continue
        seen.add(k)
        out.append(it)
    return out


async def _ssrf_guard(request: httpx.Request) -> None:
    """SSRF egress guard: reject any request whose host resolves to an internal/loopback/metadata
    address. Runs on EVERY request — including each redirect hop (httpx fires the request hook per hop)
    — so a public list URL can't 3xx-bounce the fetcher onto 169.254.169.254 / RFC-1918. These list
    URLs are user-supplied (any authenticated user), so without this the fetch is a classic SSRF."""
    netguard.assert_public_url(str(request.url))


def _client():
    # follow_redirects stays on (some providers legitimately redirect); the per-request guard above
    # re-validates every hop, so redirects can't be used to reach an internal target.
    return telemetry.instrument("metadata", timeout=_TIMEOUT, follow_redirects=True,
                                event_hooks={"request": [_ssrf_guard]})


# --------------------------------------------------------------------- AniList
_ANILIST_API = "https://graphql.anilist.co"
_ANILIST_PER_PAGE = 50
# Page-wrapped mediaList query so a large list paginates (MediaListCollection returns everything at once
# and chokes on big lists). status_in filters server-side when a list_name is given.
_ANILIST_Q = (
    "query($name:String,$page:Int,$per:Int,$status:[MediaListStatus]){ Page(page:$page, perPage:$per){ "
    "pageInfo{ hasNextPage } mediaList(userName:$name, type:MANGA, status_in:$status){ status "
    "media{ id format title{ english romaji } staff(perPage:2,sort:RELEVANCE){ nodes{ name{ full } } } "
    "} } } }"
)


async def _anilist(ref: str, list_name: str | None, config: dict) -> list[ListItem]:
    want = (list_name or "").upper().strip()
    out: list[ListItem] = []
    async with _client() as client:
        for page in range(1, MAX_PAGES + 1):
            variables = {"name": ref, "page": page, "per": _ANILIST_PER_PAGE,
                         "status": [want] if want else None}
            try:
                r = await client.post(_ANILIST_API, json={"query": _ANILIST_Q, "variables": variables},
                                      headers={"Accept": "application/json", "User-Agent": _UA})
            except httpx.HTTPError as exc:
                raise ListImportError(f"AniList unreachable ({exc})") from exc
            if r.status_code == 404:
                raise ListImportError(f"AniList user {ref!r} not found")
            if r.status_code != 200:
                raise ListImportError(f"AniList returned HTTP {r.status_code}")
            data = ((r.json() or {}).get("data") or {}).get("Page")
            if data is None:
                raise ListImportError("AniList list is private or empty")
            for e in data.get("mediaList") or []:
                m = e.get("media") or {}
                t = m.get("title") or {}
                title = (t.get("english") or t.get("romaji") or "").strip()
                if not title:
                    continue
                staff = [(s.get("name") or {}).get("full") for s in ((m.get("staff") or {}).get("nodes") or [])]
                author = next((s for s in staff if s), None)
                fmt = (m.get("format") or "").upper()
                mk = "text" if fmt in ("NOVEL",) else "comic"
                out.append(ListItem(title=title, author=author, media_kind=mk, ext_id=str(m.get("id") or "")))
            if not (data.get("pageInfo") or {}).get("hasNextPage"):
                break
            if len(out) >= MAX_LIST_ITEMS:
                log.warning("AniList list %r truncated at %d items (cap)", ref, len(out))
                break
        else:
            log.warning("AniList list %r hit page cap (%d) — possibly truncated", ref, MAX_PAGES)
    return out


# --------------------------------------------------------------------- Goodreads (public shelf RSS)
def _goodreads_id(ref: str) -> str:
    m = re.search(r"/(?:user/show/|review/list(?:_rss)?/)?(\d+)", ref) or re.search(r"(\d+)", ref)
    if not m:
        raise ListImportError("Goodreads needs your numeric user ID (from your profile URL).")
    return m.group(1)


# Listopia lists (goodreads.com/list/show/<id>) can be enormous (e.g. "Best Books Ever" ≈ 76k titles),
# so they get their own, larger page bound. The list HTML is ~100 books/page.
MAX_PAGES_LISTOPIA = 900
MAX_LIST_ITEMS_LISTOPIA = 100000


def _parse_listopia(html: str) -> list[ListItem]:
    """Parse one Goodreads Listopia page into ListItems (title + author + cover + book id)."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "lxml")
    out: list[ListItem] = []
    for tr in soup.select('tr[itemtype*="schema.org/Book"]'):
        a = tr.select_one("a.bookTitle")
        if not a:
            continue
        title = re.sub(r"\s*\([^)]*#\d+[^)]*\)\s*$", "", a.get_text(" ", strip=True)).strip()  # drop "(Series #n)"
        if not title:
            continue
        au = tr.select_one("a.authorName")
        img = tr.select_one("img.bookCover")
        cover = re.sub(r"\._S[XY]\d+_\.", "._SX200_.", img["src"]) if (img and img.get("src")) else None
        m = re.search(r"/book/show/(\d+)", a.get("href") or "")
        out.append(ListItem(title=title, author=(au.get_text(" ", strip=True) if au else None),
                            ext_id=(m.group(1) if m else None), cover_url=cover))
    return out


async def _goodreads_listopia(list_id: str, config: dict, *, limit: int | None = None) -> list[ListItem]:
    """Scrape a PUBLIC Goodreads Listopia list (/list/show/<id>) — paginated HTML, ~100 books/page.
    Bounded + paced; a partial read heals on the next scan (the item-cache upsert is idempotent).
    ``limit`` stops early (preview sample) so the add dialog doesn't wait on a full multi-hundred-page scrape."""
    import asyncio
    base = f"https://www.goodreads.com/list/show/{list_id}"
    headers = {"User-Agent": _BROWSER_UA, "Accept-Language": "en-US,en;q=0.9"}
    out: list[ListItem] = []
    prev_first: str | None = None
    misses = 0
    async with _client() as client:
        for page in range(1, MAX_PAGES_LISTOPIA + 1):
            try:
                r = await client.get(base, params={"page": page}, headers=headers)
                ok = r.status_code == 200
            except httpx.HTTPError as exc:
                if page == 1:
                    raise ListImportError(f"Goodreads unreachable ({exc})") from exc
                ok = False
            if not ok:
                if page == 1:
                    raise ListImportError("Couldn't read the Goodreads list — check the URL is public.")
                misses += 1
                if misses >= 3:
                    break   # a few consecutive page failures → stop, keep what we have
                continue
            page_items = _parse_listopia(r.text)
            if not page_items:
                break   # no book rows → past the end
            if page_items[0].title and page_items[0].title == prev_first:
                break   # server re-served the same page (ignored &page=) → stop
            prev_first = page_items[0].title
            misses = 0
            out.extend(page_items)
            if limit and len(out) >= limit:
                return out[:limit]   # preview sample collected → stop early (no warning; not a truncation)
            if len(out) >= MAX_LIST_ITEMS_LISTOPIA:
                log.warning("Goodreads list %s truncated at %d items (cap)", list_id, len(out))
                break
            await asyncio.sleep(0.15)   # be polite across hundreds of pages
        else:
            log.warning("Goodreads list %s hit page cap (%d) — possibly truncated", list_id, MAX_PAGES_LISTOPIA)
    if not out:
        raise ListImportError("That Goodreads list looks empty or isn't public.")
    return out


async def _goodreads(ref: str, list_name: str | None, config: dict,
                     limit: int | None = None) -> list[ListItem]:
    # A Listopia list URL (/list/show/<id>) is a curated public list, not a user's shelf — route it to
    # the HTML scraper. (Checked FIRST: the bare-digits fallback in _goodreads_id would misread the
    # list id as a user id.)
    lm = re.search(r"/list/show/(\d+)", ref)
    if lm:
        return await _goodreads_listopia(lm.group(1), config, limit=limit)
    uid = _goodreads_id(ref)
    shelf = (list_name or "to-read").strip()
    base = f"https://www.goodreads.com/review/list_rss/{uid}?shelf={shelf}"
    out: list[ListItem] = []
    prev_first: str | None = None   # dup-guard: a server that ignores &page= re-serves page 1 forever
    async with _client() as client:
        for page in range(1, MAX_PAGES + 1):
            try:
                r = await client.get(f"{base}&page={page}", headers={"User-Agent": _UA})
            except httpx.HTTPError as exc:
                raise ListImportError(f"Goodreads unreachable ({exc})") from exc
            if r.status_code != 200 or "<rss" not in r.text[:300].lower():
                if page == 1:
                    raise ListImportError(
                        "Couldn't read the Goodreads shelf RSS — check the ID and that it's public.")
                break   # a later page failing just means we've run past the shelf
            entries = feedparser.parse(r.text).entries
            if not entries:
                break   # empty page → end of shelf
            first_id = str(entries[0].get("book_id") or entries[0].get("title") or "")
            if first_id and first_id == prev_first:
                break   # page repeated the previous page's first item → server ignored &page=
            prev_first = first_id
            for e in entries:
                title = re.sub(r"\s*\([^)]*#\d+[^)]*\)\s*$", "", (e.get("title") or "")).strip()  # drop "(Series #n)"
                if not title:
                    continue
                cover = (e.get("book_large_image_url") or e.get("book_image_url") or "").strip() or None
                out.append(ListItem(title=title, author=(e.get("author_name") or "").strip() or None,
                                    ext_id=str(e.get("book_id") or ""), cover_url=cover))
            if limit and len(out) >= limit:
                return out[:limit]   # preview sample collected → stop early
            if len(entries) < 100:
                break   # short page (< the ~100/page size) → last page
            if len(out) >= MAX_LIST_ITEMS:
                log.warning("Goodreads shelf %s/%s truncated at %d items (cap)", uid, shelf, len(out))
                break
        else:
            log.warning("Goodreads shelf %s/%s hit page cap (%d) — possibly truncated", uid, shelf, MAX_PAGES)
    return out


# --------------------------------------------------------------------- Open Library reading log
async def _openlibrary(ref: str, list_name: str | None, config: dict) -> list[ListItem]:
    shelf = (list_name or "want-to-read").strip()
    user = ref.strip().lstrip("@")
    url = f"https://openlibrary.org/people/{user}/books/{shelf}.json"
    out: list[ListItem] = []
    async with _client() as client:
        for page in range(1, 21):
            try:
                r = await client.get(url, params={"page": page}, headers={"User-Agent": _UA})
            except httpx.HTTPError as exc:
                raise ListImportError(f"Open Library unreachable ({exc})") from exc
            if r.status_code == 404:
                raise ListImportError(f"Open Library user {user!r} / shelf {shelf!r} not found or private")
            if r.status_code != 200:
                raise ListImportError(f"Open Library returned HTTP {r.status_code}")
            entries = (r.json() or {}).get("reading_log_entries") or []
            if not entries:
                break
            for e in entries:
                w = e.get("work") or {}
                title = (w.get("title") or "").strip()
                if not title:
                    continue
                authors = w.get("author_names") or []
                out.append(ListItem(title=title, author=(authors[0] if authors else None),
                                    ext_id=w.get("key")))
            if len(entries) < 25:
                break
    return out


# --------------------------------------------------------------------- Hardcover (GraphQL user_books)
_HC_API = "https://api.hardcover.app/v1/graphql"
_HC_STATUS = {"want": 1, "reading": 2, "read": 3}
_HC_PAGE = 100
# offset/limit paginated user_books (ordered for a stable window across pages).
_HC_Q = (
    "query($u:citext!,$s:Int,$limit:Int,$offset:Int){ users(where:{username:{_eq:$u}}, limit:1){ user_books("
    "where:{status_id:{_eq:$s}}, order_by:{id:asc}, limit:$limit, offset:$offset){ "
    "book{ title contributions{ author{ name } } } } } }"
)


async def _hardcover(ref: str, list_name: str | None, config: dict) -> list[ListItem]:
    token = (config.get("hc_token") or "").strip()
    if not token:
        raise ListImportError("Hardcover import needs a Hardcover integration token configured.")
    from ..integrations.metadata import _hc_norm_token
    status = _HC_STATUS.get((list_name or "want").strip(), 1)
    auth = {"Authorization": f"Bearer {_hc_norm_token(token)}",
            "Accept": "application/json", "User-Agent": _UA}
    out: list[ListItem] = []
    async with _client() as client:
        for page in range(MAX_PAGES):
            offset = page * _HC_PAGE
            variables = {"u": ref, "s": status, "limit": _HC_PAGE, "offset": offset}
            try:
                r = await client.post(_HC_API, json={"query": _HC_Q, "variables": variables}, headers=auth)
            except httpx.HTTPError as exc:
                raise ListImportError(f"Hardcover unreachable ({exc})") from exc
            if r.status_code != 200:
                raise ListImportError(f"Hardcover returned HTTP {r.status_code}")
            data = r.json() or {}
            if data.get("errors"):
                raise ListImportError(f"Hardcover error: {(data['errors'] or [{}])[0].get('message')}")
            users = (data.get("data") or {}).get("users") or []
            if not users:
                raise ListImportError(f"Hardcover user {ref!r} not found")
            books = users[0].get("user_books") or []
            for ub in books:
                b = ub.get("book") or {}
                title = (b.get("title") or "").strip()
                if not title:
                    continue
                authors = [(c.get("author") or {}).get("name") for c in (b.get("contributions") or [])]
                out.append(ListItem(title=title, author=next((a for a in authors if a), None)))
            if len(books) < _HC_PAGE:
                break   # short/empty page → last page
            if len(out) >= MAX_LIST_ITEMS:
                log.warning("Hardcover list %r truncated at %d items (cap)", ref, len(out))
                break
        else:
            log.warning("Hardcover list %r hit page cap (%d) — possibly truncated", ref, MAX_PAGES)
    return out


# --------------------------------------------------------------------- MyAnimeList (Jikan)
async def _mal(ref: str, list_name: str | None, config: dict) -> list[ListItem]:
    url = f"https://api.jikan.moe/v4/users/{ref.strip()}/mangalist"
    want = (list_name or "").strip().lower()
    out: list[ListItem] = []
    async with _client() as client:
        for page in range(1, 11):
            try:
                params = {"page": page}
                if want:
                    params["status"] = want
                r = await client.get(url, params=params, headers={"User-Agent": _UA})
            except httpx.HTTPError as exc:
                raise ListImportError(f"MyAnimeList (Jikan) unreachable ({exc})") from exc
            if r.status_code == 404:
                raise ListImportError(f"MyAnimeList user {ref!r} not found or list is private")
            if r.status_code == 429:
                break   # Jikan rate cap — take what we have
            if r.status_code != 200:
                raise ListImportError(f"MyAnimeList (Jikan) returned HTTP {r.status_code}")
            d = r.json() or {}
            data = d.get("data") or []
            if not data:
                break
            for e in data:
                m = e.get("entry") or e.get("manga") or {}
                title = (m.get("title") or "").strip()
                if title:
                    out.append(ListItem(title=title, media_kind="comic", ext_id=str(m.get("mal_id") or "")))
            if not ((d.get("pagination") or {}).get("has_next_page")):
                break
    return out


# --------------------------------------------------------------------- Amazon public wishlist (scrape)
_AMZN = "https://www.amazon.com"


async def _amazon_wishlist(ref: str, list_name: str | None, config: dict) -> list[ListItem]:
    """Scrape ALL items from a PUBLIC Amazon wishlist. The page only renders ~10 items and lazy-loads
    the rest via /hz/wishlist/slv/items?...&paginationToken=<TOK> — so we follow that 'showMoreUrl'
    chain (the plain ?lek= on the list URL just re-serves page 1). Best-effort + bounded."""
    from urllib.parse import urlsplit, urlunsplit
    if ref.startswith("http"):
        sp = urlsplit(ref)
        # Allowlist the host to Amazon only (defense-in-depth on top of the netguard egress hook): this
        # provider takes a FULL user URL, so without it the wishlist importer is a generic URL fetcher.
        host = (sp.hostname or "").lower()
        if not re.search(r"(?:^|\.)amazon\.[a-z]{2,3}(?:\.[a-z]{2,3})?$", host):
            raise ListImportError("Amazon wishlist URL must be on an amazon.com (or regional amazon.*) host.")
        url = urlunsplit((sp.scheme, sp.netloc, sp.path, "", ""))   # drop ?ref_=wl_share etc.
    else:
        url = f"{_AMZN}/hz/wishlist/ls/{ref}"
    headers = {"User-Agent": _BROWSER_UA, "Accept-Language": "en-US,en;q=0.9"}
    out: list[ListItem] = []
    seen_tok: set[str] = set()
    async with _client() as client:
        try:
            r = await client.get(url, headers=headers)
        except httpx.HTTPError as exc:
            raise ListImportError(f"Amazon unreachable ({exc})") from exc
        if r.status_code != 200:
            raise ListImportError(f"Amazon wishlist returned HTTP {r.status_code} (is the list public?)")
        out.extend(_parse_amazon_wishlist(r.text, strict=True))
        more = _amazon_more_url(r.text)
        for _ in range(120):   # cap: ~120 pages * 10 ≈ 1200 items
            if not more:
                break
            tok = re.search(r"paginationToken=([^&\"]+)", more)
            if not tok or tok.group(1) in seen_tok:
                break   # no token / loop guard
            seen_tok.add(tok.group(1))
            try:
                r = await client.get(_AMZN + more, headers={**headers, "X-Requested-With": "XMLHttpRequest"})
            except httpx.HTTPError:
                break
            if r.status_code != 200:
                break
            page = _parse_amazon_wishlist(r.text, strict=False)
            if not page:
                break
            out.extend(page)
            more = _amazon_more_url(r.text)
    return out


def _amazon_more_url(html: str) -> str | None:
    m = re.search(r'"showMoreUrl"\s*:\s*"([^"]+)"', html)
    return m.group(1).replace("&amp;", "&").replace("\\u0026", "&").replace("\\/", "/") if m else None


def _parse_amazon_wishlist(html: str, *, strict: bool = True) -> list[ListItem]:
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "lxml")
    images = {el.get("id", "").replace("itemImage_", ""): el for el in soup.select("[id^=itemImage_]")}
    out: list[ListItem] = []
    for el in soup.select("[id^=itemName_]"):
        title = el.get_text(" ", strip=True)
        if not title:
            continue
        eid = (el.get("id") or "").replace("itemName_", "")
        author = None
        by = soup.select_one(f"#item-byline_{eid}") if eid else None
        if by:
            author = re.sub(r"^\s*by\s+", "", by.get_text(" ", strip=True), flags=re.I).split(" (")[0] or None
        cover = None
        img = images.get(eid)
        tag = img.find("img") if img else None
        if tag:
            cover = tag.get("data-a-hires") or tag.get("src")
            if cover:
                cover = re.sub(r"\._[A-Z]{1,2}\d+_\.", "._SS300_.", cover)   # upsize the thumbnail
        out.append(ListItem(title=title.split(" (")[0].strip(), author=author, cover_url=cover))
    if strict and not out and "wishlist" not in html.lower():
        raise ListImportError("Couldn't read the Amazon wishlist — make sure the list is set to PUBLIC.")
    return out


_FETCHERS = {
    "anilist": _anilist,
    "goodreads": _goodreads,
    "openlibrary": _openlibrary,
    "hardcover": _hardcover,
    "mal": _mal,
    "amazon_wishlist": _amazon_wishlist,
}


# --------------------------------------------------------------------- sync (monitor + acquire)
async def _handle_series(db, sub, row) -> None:
    """For a freshly-fetched list title that belongs to a series, optionally (per the sub's flags):
      * auto_series        — fetch the REST of the series now (series.acquire_series want_all);
      * auto_follow_series — start a series follow (Subscription) so FUTURE volumes auto-fetch,
                             seeded with the current roster so the backlog isn't re-requested.
    Best-effort + isolated: a series-handling failure must never fail the list title's own fetch."""
    from sqlalchemy import select
    from .extract import norm_title
    from .series import acquire_series, detect_series
    from ..models import Subscription
    try:
        detected = await detect_series(db, row)
    except Exception:  # noqa: BLE001
        db.rollback()
        return
    name = (detected or {}).get("series")
    if not name:
        return   # not part of a (multi-volume) series
    if sub.auto_series:
        try:
            await acquire_series(db, row, refs=None, want_all=True, user_id=sub.user_id,
                                 shelf_id=sub.target_shelf_id, origin=f"list:{sub.provider}")
        except Exception:  # noqa: BLE001
            db.rollback()
            log.exception("sync_list: series expand failed for %r (sub %s)", name, getattr(sub, "id", "?"))
    if sub.auto_follow_series:
        key = norm_title(name)
        if key and not db.scalar(select(Subscription.id).where(
                Subscription.user_id == sub.user_id, Subscription.kind == "series",
                Subscription.key == key)):
            roster = sorted({norm_title(b["title"]) for b in (detected.get("books") or []) if b.get("title")})
            db.add(Subscription(user_id=sub.user_id, kind="series", key=key, display_name=name,
                                auto_request=True, known_keys=roster))
            try:
                db.commit()
            except Exception:  # noqa: BLE001 — a concurrent identical follow lost the unique race
                db.rollback()


def cache_list_items(db, sub, items: list[ListItem]) -> None:
    """Upsert the fetched list items into the ``list_subscription_items`` cache for ``sub``: insert keys
    we haven't seen, and un-remove (clear ``removed_at``) any that re-appeared. Existing rows' lightweight
    metadata (title/author/cover/ref/media_kind) is refreshed so the UI shows the current values. Caller
    commits. Never resolves work/cover — these are the LIST's own fields. Keys absent from ``items`` are
    NOT marked removed here (that's the scan's job via the diff in ``sync_list``)."""
    from sqlalchemy import select
    from .extract import norm_title
    from ..models import ListSubscriptionItem
    existing = {r.norm_key: r for r in db.scalars(
        select(ListSubscriptionItem).where(ListSubscriptionItem.subscription_id == sub.id)).all()}
    for it in items:
        if not it.title:
            continue
        key = norm_title(it.title)
        if not key:
            continue
        row = existing.get(key)
        if row is None:
            db.add(ListSubscriptionItem(
                subscription_id=sub.id, norm_key=key, title=it.title[:512], author=it.author,
                ref=it.ext_id, media_kind=it.media_kind, cover_url=it.cover_url))
            existing[key] = True   # guard against an items-list dup re-inserting the same key this pass
        elif row is not True:
            row.title = it.title[:512]
            row.author = it.author
            row.ref = it.ext_id
            row.media_kind = it.media_kind
            row.cover_url = it.cover_url
            row.removed_at = None


def cached_items(db, sub_id: int) -> list[ListItem]:
    """The currently-on-list cached items (``removed_at`` IS NULL) for a subscription, as ListItems for
    the UI. Empty when the cache hasn't been populated yet (caller can then fall back to a live fetch)."""
    from sqlalchemy import select
    from ..models import ListSubscriptionItem
    rows = db.scalars(select(ListSubscriptionItem).where(
        ListSubscriptionItem.subscription_id == sub_id,
        ListSubscriptionItem.removed_at.is_(None)).order_by(ListSubscriptionItem.id)).all()
    return [ListItem(title=r.title, author=r.author, media_kind=r.media_kind,
                     ext_id=r.ref, cover_url=r.cover_url) for r in rows]


def provider_config(db) -> dict:
    """Provider-side secrets the fetchers need but the subscription doesn't store — currently just the
    shared Hardcover token from the configured Hardcover integration."""
    from sqlalchemy import select
    from ..models import Integration
    cfg: dict = {}
    hc = db.scalar(select(Integration).where(Integration.kind == "hardcover", Integration.enabled.is_(True)))
    if hc and hc.api_key:
        cfg["hc_token"] = hc.api_key
    return cfg


# How many failed attempts before an item is given up on. The list's only job is to KICK OFF acquire
# (which opens a per-format missing-content LEDGER row that then chases each format — ebook AND
# audiobook — for weeks on its own jittered cadence), so a handful of attempts to get the title
# resolved is enough; the real long-term retry lives in the ledger, not here.
INGEST_ATTEMPTS_MAX = 5
# Retry backoff (minutes) for a still-failing item, doubling per attempt up to a daily ceiling — so an
# unresolvable title isn't re-attempted every tick.
_RETRY_BASE_MIN = 15
_RETRY_MAX_MIN = 24 * 60
# First-batch size for a manual "Check now" / the initial import, so the user sees motion immediately;
# the rest of a big list is drained incrementally by list_ingest_tick.
SYNC_NOW_BATCH = 12


def _next_retry(attempts: int) -> datetime:
    from datetime import timedelta
    mins = min(_RETRY_MAX_MIN, _RETRY_BASE_MIN * (2 ** max(0, attempts - 1)))
    return _utcnow() + timedelta(minutes=mins)


def _pending_query(sub_id: int, *, now: datetime):
    """Items still needing work: on the list, not done/skipped, under the retry cap, and past their
    backoff (a fresh `pending` item has no backoff; a re-tried `failed` one waits for next_attempt_at)."""
    from sqlalchemy import and_, or_, select
    from ..models import ListSubscriptionItem as I
    return select(I).where(
        I.subscription_id == sub_id, I.removed_at.is_(None),
        or_(I.status == "pending",
            and_(I.status == "failed", I.attempts < INGEST_ATTEMPTS_MAX,
                 or_(I.next_attempt_at.is_(None), I.next_attempt_at <= now))),
    ).order_by(I.id)


def pending_count(db, sub) -> int:
    """How many of `sub`'s items are eligible to process right now (honours the cap + backoff)."""
    from sqlalchemy import and_, func, or_, select
    from ..models import ListSubscriptionItem as I
    now = _utcnow()
    return int(db.scalar(select(func.count()).select_from(I).where(
        I.subscription_id == sub.id, I.removed_at.is_(None),
        or_(I.status == "pending",
            and_(I.status == "failed", I.attempts < INGEST_ATTEMPTS_MAX,
                 or_(I.next_attempt_at.is_(None), I.next_attempt_at <= now))))) or 0)


def progress(db, sub_id: int) -> dict:
    """Per-subscription ingest progress for the UI: counts by terminal/working state."""
    from sqlalchemy import func, select
    from ..models import ListSubscriptionItem as I
    rows = db.execute(select(I.status, func.count()).where(
        I.subscription_id == sub_id, I.removed_at.is_(None)).group_by(I.status)).all()
    by = {s: int(n) for s, n in rows}
    return {"total": sum(by.values()), "done": by.get("done", 0),
            "pending": by.get("pending", 0) + by.get("failed", 0), "skipped": by.get("skipped", 0)}


async def scan_list(db, sub, *, force: bool = False) -> int:
    """Re-fetch the external list (titles/refs only — the one unavoidable network cost) and refresh the
    item cache: newly-seen titles are inserted as ``pending``, departed ones marked ``removed_at``. Does
    NOT resolve/acquire (that's ``process_pending``). To avoid re-scraping a huge, mostly-static list on
    every tick, the fetch is SKIPPED while items are still pending — unless ``force`` (manual check) or
    the cache is empty. Returns how many NEW titles were added. Raises ListImportError if unreadable."""
    from sqlalchemy import select
    from .extract import norm_title
    from ..models import ListSubscriptionItem

    have_any = db.scalar(select(ListSubscriptionItem.id).where(
        ListSubscriptionItem.subscription_id == sub.id).limit(1)) is not None
    if have_any and not force and pending_count(db, sub) > 0:
        return 0   # still draining what we already fetched → no point re-scraping yet

    items = await fetch_list(sub.provider, sub.list_ref, list_name=sub.list_name,
                             config=provider_config(db))
    by_key = {norm_title(it.title): it for it in items if it.title}
    current_set = set(by_key)
    existing = {k for (k,) in db.execute(select(ListSubscriptionItem.norm_key).where(
        ListSubscriptionItem.subscription_id == sub.id)).all()}

    cache_list_items(db, sub, items)   # inserts new rows (status defaults to 'pending'); refreshes metadata
    for r in db.scalars(select(ListSubscriptionItem).where(
            ListSubscriptionItem.subscription_id == sub.id,
            ListSubscriptionItem.removed_at.is_(None))).all():
        if r.norm_key not in current_set:
            r.removed_at = _utcnow()

    sub.last_checked_at = _utcnow()
    sub.last_error = None
    db.commit()
    return len(current_set - existing)


async def _acquire_or_stock(db, sub, owner, priority, variants, ctx, row) -> bool:
    """Download-mode handling for one resolved title: queue to operator stock (admin subs), or fire
    acquire for EACH wanted variant into the user's library. Each variant opens its own missing-content
    ledger row, so the ledger then retries each format (ebook AND audiobook) independently on its own
    cadence — the list just kicks them off. Returns True when the title was handled (so the item is
    done); False only when a stock destination can't queue it yet (retry)."""
    if getattr(sub, "to_stock", False):
        # to_stock is an admin privilege — re-verify so a downgraded account can't keep feeding the pool.
        if owner is None or owner.role != "admin":
            return False
        from . import stock as stock_mod
        gid = getattr(row, "group_id", None)
        # ponytail: gid None (catalog row not grouped yet) → return False so the item retries next tick
        # once the regroup tick assigns one; the re-resolve is catalog-cached so the churn is cheap.
        if gid is not None and stock_mod.stock_configured(db):
            stock_mod.queue_selection(db, name=sub.display_name, group_ids=[gid],
                                      variant=sub.variant or "ebook")
            return True
        return False
    from .acquire import acquire
    for v in variants:
        # Fire-and-track: acquire opens/updates the ledger row for THIS format and starts a download if
        # it can. We don't gate "done" on it succeeding now — the ledger owns the long-term retry.
        await acquire(db, row, user_id=sub.user_id, priority=priority,
                      shelf_id=sub.target_shelf_id, context=ctx, variant=v)
    return True


async def process_pending(db, sub, *, limit: int) -> int:
    """Drain up to ``limit`` not-yet-handled items: resolve each title's catalog row (its METADATA — this
    is the slow, rate-limited step that makes a 76k list ingest gradually), then in ``catalog`` mode stop
    there (the title is now browsable in Discovery) or in ``download`` mode acquire it. Each item is
    committed on its own so one failure can't stall the rest, and ``done``/``skipped`` are never revisited.
    Returns how many items reached ``done`` this run."""
    from .acquire import user_priority
    from .series import _resolve_book_row
    from ..models import ListSubscriptionItem, User

    items = db.scalars(_pending_query(sub.id, now=_utcnow()).limit(limit)).all()
    if not items:
        return 0
    owner = db.get(User, sub.user_id)
    priority = None if sub.mode == "catalog" else user_priority(db, owner)
    variants = ("ebook", "audiobook") if sub.variant == "both" else (sub.variant or "ebook",)
    ctx = {"origin": f"list:{sub.provider}", "origin_detail": sub.display_name}

    def _ok(it) -> None:
        it.status, it.resolved_at, it.last_error, it.next_attempt_at = "done", _utcnow(), None, None

    def _fail(it, msg: str) -> None:
        it.status, it.last_error = "failed", msg
        it.next_attempt_at = _next_retry(it.attempts)

    done = 0
    for it in items:
        item_id, title = it.id, it.title
        try:
            it.attempts = (it.attempts or 0) + 1
            # Pass the list item's media kind so a crawled-source match must serve that content type
            # (a manga list entry can't match a prose web-novel of the same title, and vice-versa).
            row = await _resolve_book_row(db, it.title, it.author, media_kind=it.media_kind)
            if row is None:
                _fail(it, "no catalog match yet")
            elif sub.mode == "catalog":
                # Catalogued: metadata resolved + browsable in Discovery. No download.
                it.catalog_id = row.id
                _ok(it)
                done += 1
            else:
                it.catalog_id = row.id
                # Kick off each wanted format; the ledger then retries each independently, so the item
                # is done once handed off (a stock dest that can't queue yet retries with backoff).
                if await _acquire_or_stock(db, sub, owner, priority, variants, ctx, row):
                    _ok(it)
                    done += 1
                    if sub.auto_series or sub.auto_follow_series:
                        await _handle_series(db, sub, row)
                else:
                    _fail(it, "not stockable yet")
            db.commit()
        except Exception:  # noqa: BLE001 — one title must not stall the rest
            db.rollback()
            log.exception("process_pending: %r (sub %s)", title, getattr(sub, "id", "?"))
            fresh = db.get(ListSubscriptionItem, item_id)
            if fresh is not None:
                fresh.attempts = (fresh.attempts or 0) + 1
                _fail(fresh, "error")
                db.commit()
    if done:
        sub.auto_added = (sub.auto_added or 0) + done
        db.commit()
    return done


async def sync_list(db, sub, *, seed_only: bool = False) -> int:
    """Back-compat entry for a manual 'Check now' / the initial import: re-scan the list, then process a
    small first batch so the user sees motion at once. The bulk of a big list is drained incrementally by
    list_ingest_tick. ``seed_only`` only establishes the cache (used to baseline without ingesting)."""
    await scan_list(db, sub, force=True)
    if seed_only:
        return 0
    return await process_pending(db, sub, limit=SYNC_NOW_BATCH)
