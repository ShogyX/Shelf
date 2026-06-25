"""Metadata-provider integrations: ranobedb (rich source of truth) + Goodreads (wishlist).

Unlike the download-manager integrations (Readarr/Kapowarr) these don't host files — they
provide canonical metadata, detect new releases, surface related titles, and (Goodreads)
import a user's want-to-read shelf. They implement :class:`MetadataProvider` rather than the
download-manager ``BaseClient``.

  * ranobedb.org — clean public JSON API (search + series detail with staff/volumes/relations).
  * Goodreads    — its API was discontinued; the only remaining public surface is the per-shelf
                   RSS feed, so Goodreads supports ``wanted()`` (a public shelf) but not search.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import feedparser
import httpx
from .. import telemetry

from . import IntegrationError

RANOBEDB_API = "https://ranobedb.org/api/v0"
RANOBEDB_IMG = "https://images.ranobedb.org"

# How long to stop calling a provider after a 429/503. Honour its Retry-After when given; otherwise
# default to 15 min (covers Google Books' day-long quota block — re-probing ~4×/h is not spam),
# capped at 1h so we never wait absurdly long on a misreported Retry-After.
_RL_DEFAULT_COOLDOWN_S = 900.0
_RL_MAX_COOLDOWN_S = 3600.0


def _cooldown_after(resp: httpx.Response) -> float:
    """Seconds to cool a provider down after a 429/503, from its Retry-After header (delay-seconds or
    an HTTP-date) when present, else the default — clamped to [_default? , _RL_MAX_COOLDOWN_S]."""
    ra = resp.headers.get("Retry-After") or resp.headers.get("retry-after")
    secs: float | None = None
    if ra:
        try:
            secs = float(ra)                       # delay-seconds form
        except ValueError:
            from email.utils import parsedate_to_datetime
            try:
                dt = parsedate_to_datetime(ra)     # HTTP-date form
                if dt is not None:
                    from datetime import datetime, timezone
                    secs = (dt - datetime.now(dt.tzinfo or timezone.utc)).total_seconds()
            except Exception:  # noqa: BLE001 — a malformed Retry-After just falls back to default
                secs = None
    if secs is None or secs <= 0:
        secs = _RL_DEFAULT_COOLDOWN_S
    return min(secs, _RL_MAX_COOLDOWN_S)


@dataclass
class ProviderMatch:
    ref: str
    title: str
    author: str | None = None
    year: int | None = None
    cover_url: str | None = None
    synopsis: str | None = None
    media_kind: str = "text"
    url: str | None = None


@dataclass
class RelatedWork:
    title: str
    relation: str            # prequel | sequel | side story | spin-off | …
    ref: str | None = None
    author: str | None = None


@dataclass
class ProviderMeta:
    ref: str
    title: str
    author: str | None = None
    synopsis: str | None = None
    cover_url: str | None = None
    media_kind: str = "text"
    total_units: int | None = None
    unit_kind: str = "volumes"            # volumes | chapters | pages
    status: str = "ongoing"               # ongoing | complete
    release_marker: str | None = None     # changes when a new release drops
    related: list[RelatedWork] = field(default_factory=list)
    # Discovery taxonomy (powers the Index page's genre/theme rows). genres = broad buckets
    # (Action, Romance, …); tags = finer themes (Isekai, Revenge, …). popularity = a raw audience
    # signal (e.g. AniList user count) used to rank a catalog row that matched this provider.
    genres: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    popularity: int | None = None
    is_adult: bool = False                # provider 18+ flag (AniList isAdult / GBooks MATURE)
    url: str | None = None
    # Display metadata for the library detail modal (Wave 5). rating is on the 0–10 convention.
    year: int | None = None               # first publication year
    rating: float | None = None
    rating_count: int | None = None
    publisher: str | None = None
    extra: dict = field(default_factory=dict)


class MetadataProvider:
    kind = "abstract"
    timeout = 20.0
    # Whether re-fetching a link can reveal a NEW release (drives the release-watch pass). A
    # single-edition source like Google Books never changes, so watching it just wastes calls.
    tracks_releases = False
    # Whether fetching uses a (slow) headless browser render. The on-hook enrich path skips these
    # so a render can't stall bulk auto-hooking; the periodic sweep still covers them.
    renders = False

    def __init__(self, base_url: str = "", api_key: str = "", config: dict | None = None) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.config = config or {}
        # Per-provider request limit + timeout: operator override (config) or catalog default. The
        # class-level ``timeout`` stays the fallback for the abstract base / unknown kinds.
        from .provider_catalog import resolve_limits
        self._rpm, self._timeout = resolve_limits(self.kind, self.config)

    async def _request(self, method: str, url: str, **kw):
        import asyncio
        from urllib.parse import urljoin

        from ..ingestion.netguard import _MAX_REDIRECT_HOPS, _pin_to_ip, BlockedAddress, assert_public_url
        from . import ratelimit
        # Reactive cooldown: if this provider recently returned 429/503 (rate-limit / daily quota /
        # unavailable), DON'T call it again until the cooldown lapses — raise as a transient so the
        # caller leaves the work due for a later retry, but make NO upstream request (no spamming).
        cd = ratelimit.cooling_down(self.kind)
        if cd > 0:
            raise IntegrationError(f"{self.kind}: rate-limit cooldown, ~{cd/60:.0f}m remaining")
        # Politeness throttle so a bulk sweep can't trip the provider's rate limit / Cloudflare.
        await ratelimit.throttle(self.kind, self._rpm)
        headers = {"User-Agent": "Mozilla/5.0 (compatible; ShelfReader/0.1)",
                   "Accept": "application/json, */*", **kw.pop("headers", {})}
        # SSRF guard: the base URL / Goodreads user id are operator-configurable, and Goodreads is
        # reachable by any logged-in user. Auto-redirect is DISABLED and each hop is re-validated +
        # IP-pinned (a public host 302ing to 169.254.169.254 / RFC-1918 must be blocked), mirroring
        # netguard.safe_get / imagecache. DNS resolution runs off the event loop.
        cur = url
        try:
            async with telemetry.instrument("metadata", timeout=self._timeout, follow_redirects=False) as c:
                for _hop in range(_MAX_REDIRECT_HOPS + 1):
                    try:
                        ips = await asyncio.to_thread(assert_public_url, cur)
                    except BlockedAddress as exc:
                        raise IntegrationError(f"{self.kind}: refusing to fetch {cur}: {exc}") from exc
                    pinned_url, host_header, ext = _pin_to_ip(cur, ips[0])
                    r = await c.request(method, pinned_url, headers={**headers, **host_header},
                                        extensions=ext, **kw)
                    if r.is_redirect:
                        loc = r.headers.get("location")
                        if not loc:
                            return r
                        cur = urljoin(cur, loc)
                        continue
                    # Upstream said slow down / over quota / unavailable → put this provider on
                    # cooldown (honour Retry-After if given) so the next ticks skip it entirely.
                    if r.status_code in (429, 503):
                        ratelimit.penalize(self.kind, _cooldown_after(r))
                    return r
                raise IntegrationError(f"{self.kind}: too many redirects fetching {url}")
        except httpx.HTTPError as exc:
            raise IntegrationError(f"{self.kind}: request to {url} failed: {exc}") from exc

    async def _get(self, url: str, **kw):
        return await self._request("GET", url, **kw)

    async def _post(self, url: str, **kw):
        """POST (used by GraphQL providers). Pass ``json=...`` for a JSON body."""
        return await self._request("POST", url, **kw)

    async def test_connection(self) -> dict:  # pragma: no cover - interface
        raise NotImplementedError

    async def search(self, title: str, author: str | None = None, *, limit: int = 8
                     ) -> list[ProviderMatch]:
        return []

    async def fetch(self, ref: str) -> ProviderMeta | None:
        return None

    async def fetch_many(self, refs: list[str]) -> dict[str, ProviderMeta | None]:
        """Fetch metadata for many refs at once. Default: sequential by-id fetch() (each call is
        already throttled by the provider's own rate limiter). Providers with a batch multi-id
        query (AniList id_in, Hardcover _in) override this to cut N round-trips to ~N/page (14B).
        An IntegrationError propagates (a quota/block hits every ref); other per-ref errors → None."""
        out: dict[str, ProviderMeta | None] = {}
        for ref in refs:
            try:
                out[ref] = await self.fetch(ref)
            except IntegrationError:
                raise
            except Exception:  # noqa: BLE001 — one bad ref shouldn't abort the batch
                out[ref] = None
        return out

    async def wanted(self) -> list[ProviderMatch]:
        """Items the user wants (e.g. a Goodreads shelf). Empty for providers without one."""
        return []


# --------------------------------------------------------------------- ranobedb
def _strip_series_suffix(title: str) -> str:
    """'Title (Series, #3)' / 'Title, Vol. 2' → a cleaner base title for matching."""
    t = re.sub(r"\s*\([^)]*#[^)]*\)\s*$", "", title or "")
    t = re.sub(r"[,:]?\s*(vol(?:ume)?\.?|book|part)\s*\d+.*$", "", t, flags=re.I)
    return t.strip() or (title or "")


class RanobeDbProvider(MetadataProvider):
    kind = "ranobedb"
    tracks_releases = True  # series feed: new volumes advance the release marker

    def __init__(self, base_url: str = "", api_key: str = "", config: dict | None = None) -> None:
        super().__init__(base_url or RANOBEDB_API, api_key, config)
        if "/api/" not in self.base_url:
            self.base_url = self.base_url + "/api/v0"

    async def test_connection(self) -> dict:
        r = await self._get(f"{self.base_url}/series?q=bookworm&limit=1")
        if r.status_code != 200:
            raise IntegrationError(f"ranobedb returned HTTP {r.status_code}")
        return {"ok": True, "app": "RanobeDB", "version": None}

    async def search(self, title: str, author: str | None = None, *, limit: int = 8
                     ) -> list[ProviderMatch]:
        from urllib.parse import quote
        r = await self._get(f"{self.base_url}/series?q={quote(title)}&limit={limit}")
        # A non-200 here is an API-level failure (rate limit / block / 5xx), NOT "0 results" —
        # raise so the caller records it instead of silently treating it as no matches found.
        if r.status_code != 200:
            raise IntegrationError(f"ranobedb search HTTP {r.status_code}: {r.text[:200]}")
        out: list[ProviderMatch] = []
        for s in (r.json() or {}).get("series", []) or []:
            img = (s.get("book") or {}).get("image") or s.get("image") or {}
            out.append(ProviderMatch(
                ref=str(s.get("id")),
                title=s.get("title") or s.get("romaji") or s.get("title_orig") or "",
                year=int(str(s.get("c_start_date") or "0")[:4] or 0) or None,
                cover_url=f"{RANOBEDB_IMG}/{img['filename']}" if img.get("filename") else None,
                url=f"https://ranobedb.org/series/{s.get('id')}",
            ))
        return out

    async def fetch(self, ref: str) -> ProviderMeta | None:
        r = await self._get(f"{self.base_url}/series/{ref}")
        if r.status_code == 404:
            return None  # the series genuinely went away — not an API failure
        if r.status_code != 200:
            raise IntegrationError(f"ranobedb fetch HTTP {r.status_code}: {r.text[:200]}")
        s = (r.json() or {}).get("series")
        if not isinstance(s, dict) or not s.get("id"):
            return None
        staff = s.get("staff") or []
        author = next((p.get("name") for p in staff if (p.get("role_type") or "").lower() == "author"), None)
        books = s.get("books") or []
        cover = None
        if books:
            img = books[0].get("image") or {}
            if img.get("filename"):
                cover = f"{RANOBEDB_IMG}/{img['filename']}"
        latest = max((b.get("c_release_date") or 0) for b in books) if books else 0
        total = len(books) or s.get("c_num_books")
        status = "complete" if (s.get("publication_status") == "completed") else "ongoing"
        related = [
            RelatedWork(title=c.get("title") or "", relation=(c.get("relation_type") or "related"),
                        ref=str(c.get("id")) if c.get("id") else None)
            for c in (s.get("child_series") or []) if c.get("title")
        ]
        return ProviderMeta(
            ref=str(s["id"]),
            title=s.get("title") or s.get("romaji") or s.get("title_orig") or "",
            author=author,
            synopsis=(s.get("description") or "").strip() or None,
            cover_url=cover,
            media_kind="text",
            total_units=total,
            unit_kind="volumes",
            status=status,
            # Marker changes when a new volume is added OR the latest release date advances.
            release_marker=f"{total or 0}:{latest or 0}",
            related=related,
            url=f"https://ranobedb.org/series/{s['id']}",
            extra={"aliases": s.get("aliases"), "anilist_id": s.get("anilist_id"),
                   "mal_id": s.get("mal_id")},
        )


# --------------------------------------------------------------------- goodreads
class GoodreadsProvider(MetadataProvider):
    """Goodreads has no API anymore; we read a public shelf's RSS feed. config:
    {"user_id": "<numeric id>", "shelf": "to-read"}."""

    kind = "goodreads"

    def _shelf_url(self) -> str:
        uid = str(self.config.get("user_id") or self.base_url or "").strip().rstrip("/")
        m = re.search(r"/(?:user/show/|review/list(?:_rss)?/)?(\d+)", uid) or re.search(r"(\d+)", uid)
        if not m:
            raise IntegrationError(
                "Goodreads needs your numeric user ID (from your profile URL, e.g. "
                "goodreads.com/user/show/12345-name)."
            )
        shelf = (self.config.get("shelf") or "to-read").strip()
        return f"https://www.goodreads.com/review/list_rss/{m.group(1)}?shelf={shelf}"

    async def test_connection(self) -> dict:
        r = await self._get(self._shelf_url())
        if r.status_code != 200 or "<rss" not in r.text[:200].lower():
            raise IntegrationError(
                "Couldn't read the Goodreads shelf RSS — check the user ID and that the shelf is public."
            )
        feed = feedparser.parse(r.text)
        return {"ok": True, "app": "Goodreads", "version": None,
                "detail": f"{len(feed.entries)} books on shelf '{self.config.get('shelf') or 'to-read'}'"}

    async def wanted(self) -> list[ProviderMatch]:
        r = await self._get(self._shelf_url())
        if r.status_code != 200:
            raise IntegrationError(f"Goodreads shelf RSS returned HTTP {r.status_code}")
        feed = feedparser.parse(r.text)
        out: list[ProviderMatch] = []
        for e in feed.entries:
            cover = (e.get("book_large_image_url") or e.get("book_medium_image_url")
                     or e.get("book_image_url") or "").strip() or None
            out.append(ProviderMatch(
                ref=str(e.get("book_id") or e.get("id") or ""),
                title=_strip_series_suffix(e.get("title") or ""),
                author=(e.get("author_name") or "").strip() or None,
                cover_url=cover,
                synopsis=re.sub(r"<[^>]+>", " ", e.get("book_description") or "").strip() or None,
                url=e.get("link"),
            ))
        return out


# --------------------------------------------------------------------- google books
GOOGLE_BOOKS_API = "https://www.googleapis.com/books/v1"


def _gb_year(d: str | None) -> int | None:
    m = re.match(r"(\d{4})", d or "")
    return int(m.group(1)) if m else None


# Largest → smallest. The volume GET exposes small/medium/large/extraLarge; search results often
# carry only thumbnail/smallThumbnail. We pick the best present, then force full resolution below.
_GB_COVER_KEYS = ("extraLarge", "large", "medium", "small", "thumbnail", "smallThumbnail")


def _gb_cover(links: dict | None) -> str | None:
    """Best-resolution Google Books cover. The default `thumbnail` is a ~128px crop; the cover
    `content` endpoint instead serves the FULL original at `zoom=0` (≈1500-2600px) — the `imgtk`
    token isn't zoom-locked, so we keep whichever link is present and force `zoom=0`. Also forces
    https and drops the `edge=curl` page-curl overlay (without leaving a dangling separator)."""
    if not links:
        return None
    url = ""
    for k in _GB_COVER_KEYS:
        cand = (links.get(k) or "").strip()
        if cand:
            url = cand
            break
    if not url:
        return None
    url = url.replace("http://", "https://").replace("&edge=curl", "").replace("edge=curl", "")
    # Request the original full-resolution scan rather than the tiny default thumbnail.
    if re.search(r"[?&]zoom=\d+", url):
        url = re.sub(r"([?&]zoom=)\d+", r"\g<1>0", url)
    elif "/content" in url:
        url += ("&" if "?" in url else "?") + "zoom=0"
    while "&&" in url or "?&" in url:  # collapse any separators left by removing a param
        url = url.replace("&&", "&").replace("?&", "?")
    return url.rstrip("?&")


def _gb_thumb(links: dict | None) -> str | None:
    """The plain thumbnail (real, low-res) — the safe fallback when the full-res `_gb_cover` URL
    turns out to be Google's 'image not available' placeholder. Unlike `_gb_cover` it never forces
    zoom=0, so for a thumbnail-only volume it returns the genuine (small) cover, not the placeholder."""
    if not links:
        return None
    url = (links.get("thumbnail") or links.get("smallThumbnail") or "").strip()
    if not url:
        return None
    url = url.replace("http://", "https://").replace("&edge=curl", "").replace("edge=curl", "")
    while "&&" in url or "?&" in url:
        url = url.replace("&&", "&").replace("?&", "?")
    return url.rstrip("?&")


def _gb_media_kind(categories: list | None) -> str:
    cats = " ".join(str(c) for c in (categories or [])).lower()  # tolerate non-string entries
    return "comic" if ("comic" in cats or "graphic novel" in cats or "manga" in cats) else "text"


class GoogleBooksProvider(MetadataProvider):
    """Google Books Volumes API — broad coverage for prose fiction (and many comics) that
    ranobedb (light-novel focused) doesn't carry. Public API; an API key is optional and only
    raises the rate limit. Returns single editions, so there's no series release-tracking —
    its value is canonical author / synopsis / cover for matched works."""

    kind = "googlebooks"

    def __init__(self, base_url: str = "", api_key: str = "", config: dict | None = None) -> None:
        super().__init__(base_url or GOOGLE_BOOKS_API, api_key, config)
        if "/books/" not in self.base_url:
            self.base_url = self.base_url + "/books/v1"

    def _url(self, path: str, **params) -> str:
        from urllib.parse import urlencode
        if self.api_key:
            params["key"] = self.api_key
        qs = urlencode({k: v for k, v in params.items() if v not in (None, "")})
        return f"{self.base_url}{path}" + (f"?{qs}" if qs else "")

    async def test_connection(self) -> dict:
        r = await self._get(self._url("/volumes", q="bookworm", maxResults=1))
        if r.status_code != 200:
            raise IntegrationError(f"Google Books returned HTTP {r.status_code}")
        return {"ok": True, "app": "Google Books", "version": None}

    async def search(self, title: str, author: str | None = None, *, limit: int = 8
                     ) -> list[ProviderMatch]:
        # `inauthor:` biases Google toward the right book without hard-filtering (our own
        # _confidence re-scores), and printType=books drops magazines from the candidates.
        q = f"{title} inauthor:{author}" if author else title
        r = await self._get(self._url("/volumes", q=q, maxResults=limit, printType="books"))
        # Surface API failures (commonly HTTP 429 "quota exceeded" on the keyless shared quota)
        # rather than masking them as an empty result set. Add an API key to raise the quota.
        if r.status_code != 200:
            raise IntegrationError(f"Google Books search HTTP {r.status_code}: {r.text[:200]}")
        out: list[ProviderMatch] = []
        for it in (r.json() or {}).get("items", []) or []:
            vi = it.get("volumeInfo") or {}
            if not vi.get("title") or not it.get("id"):
                continue
            out.append(ProviderMatch(
                ref=str(it["id"]),
                title=vi.get("title") or "",
                author=", ".join(vi.get("authors") or []) or None,
                year=_gb_year(vi.get("publishedDate")),
                cover_url=_gb_cover(vi.get("imageLinks")),
                synopsis=(vi.get("description") or "").strip() or None,
                media_kind=_gb_media_kind(vi.get("categories")),
                url=vi.get("infoLink") or vi.get("canonicalVolumeLink"),
            ))
        return out

    async def fetch(self, ref: str) -> ProviderMeta | None:
        r = await self._get(self._url(f"/volumes/{ref}"))
        if r.status_code == 404:
            return None  # the volume genuinely went away — not an API failure
        if r.status_code != 200:
            raise IntegrationError(f"Google Books fetch HTTP {r.status_code}: {r.text[:200]}")
        it = r.json() or {}
        vi = it.get("volumeInfo") or {}
        if not it.get("id") or not vi.get("title"):
            return None
        pages = vi.get("pageCount")
        published = vi.get("publishedDate")
        return ProviderMeta(
            ref=str(it["id"]),
            title=vi.get("title") or "",
            author=", ".join(vi.get("authors") or []) or None,
            synopsis=(vi.get("description") or "").strip() or None,
            cover_url=_gb_cover(vi.get("imageLinks")),
            media_kind=_gb_media_kind(vi.get("categories")),
            total_units=int(pages) if isinstance(pages, int) and pages > 0 else None,
            unit_kind="pages",
            # A published edition is a finished artifact — and Google Books has no series feed,
            # so the marker is stable (release-watch is a no-op for this provider).
            status="complete",
            release_marker=f"gb:{published}" if published else None,
            is_adult=(vi.get("maturityRating") or "").upper() == "MATURE",
            url=vi.get("infoLink") or vi.get("canonicalVolumeLink"),
            year=_gb_year(published),
            # Google rates 0–5 → normalize to the 0–10 convention the UI shows.
            rating=round(float(vi["averageRating"]) * 2.0, 2)
            if isinstance(vi.get("averageRating"), (int, float)) else None,
            rating_count=vi.get("ratingsCount") if isinstance(vi.get("ratingsCount"), int) else None,
            publisher=(vi.get("publisher") or "").strip() or None,
            extra={"isbn": [i.get("identifier") for i in (vi.get("industryIdentifiers") or [])],
                   "categories": vi.get("categories"), "page_count": pages,
                   # Safe low-res fallback if the full-res cover is the "not available" placeholder.
                   "cover_thumb": _gb_thumb(vi.get("imageLinks"))},
        )


# --------------------------------------------------------------------- anilist
ANILIST_API = "https://graphql.anilist.co"

_ANILIST_REL = {  # AniList relationType → our human-readable relation label
    "PREQUEL": "prequel", "SEQUEL": "sequel", "SIDE_STORY": "side story",
    "SPIN_OFF": "spin-off", "ALTERNATIVE": "alternative", "PARENT": "parent story",
    "ADAPTATION": "adaptation", "SOURCE": "source",
}

# Pull title/staff/counts/relations in one round trip. `chapters` is populated once a manga is
# tracked/finished — that's the authoritative chapter count we compare against.
_ANILIST_FIELDS = """
  id format status chapters volumes isAdult
  title { romaji english native }
  description(asHtml: false)
  coverImage { extraLarge large }
  siteUrl startDate { year }
  genres averageScore popularity
  tags { name rank isGeneralSpoiler isMediaSpoiler isAdult }
  staff(perPage: 4, sort: RELEVANCE) { edges { role node { name { full } } } }
"""


def _anilist_tags(tags: list | None) -> list[str]:
    """Top non-spoiler, non-adult AniList tags (themes) by community rank — the finer 'theme'
    taxonomy behind theme rows (Isekai, Revenge, Time Travel, …)."""
    out: list[tuple[int, str]] = []
    for t in tags or []:
        name = (t or {}).get("name")
        if not name or t.get("isGeneralSpoiler") or t.get("isMediaSpoiler") or t.get("isAdult"):
            continue
        out.append((t.get("rank") or 0, name))
    out.sort(reverse=True)
    return [n for _, n in out[:8]]


def _anilist_title(t: dict | None) -> str:
    t = t or {}
    return t.get("english") or t.get("romaji") or t.get("native") or ""


def _anilist_author(staff: dict | None) -> str | None:
    edges = (staff or {}).get("edges") or []
    story = next((e for e in edges if "story" in (e.get("role") or "").lower()), None)
    pick = story or (edges[0] if edges else None)
    return (((pick or {}).get("node") or {}).get("name") or {}).get("full") if pick else None


def _anilist_media_kind(fmt: str | None) -> str:
    return "text" if (fmt or "").upper() == "NOVEL" else "comic"


class AniListProvider(MetadataProvider):
    """AniList GraphQL API (graphql.anilist.co) — a stable, key-less source of authoritative
    CHAPTER counts for manga (and some web-comics/novels). Unlike ranobedb (volumes) and Google
    Books (pages), AniList's ``chapters`` field lets us validate a work's chapter total and detect
    when more chapters exist than we've downloaded. No auth required (≈90 requests/minute)."""

    kind = "anilist"
    timeout = 15.0
    tracks_releases = True  # chapter count advances / status flips → a new release to pull

    def __init__(self, base_url: str = "", api_key: str = "", config: dict | None = None) -> None:
        super().__init__(base_url or ANILIST_API, api_key, config)

    async def _gql(self, query: str, variables: dict) -> dict:
        r = await self._post(self.base_url, json={"query": query, "variables": variables})
        if r.status_code != 200:
            raise IntegrationError(f"anilist HTTP {r.status_code}: {r.text[:200]}")
        body = r.json() or {}
        if body.get("errors"):
            raise IntegrationError(f"anilist GraphQL error: {str(body['errors'])[:200]}")
        return body.get("data") or {}

    async def test_connection(self) -> dict:
        await self._gql("query($q:String){Page(perPage:1){media(search:$q,type:MANGA){id}}}",
                        {"q": "bookworm"})
        return {"ok": True, "app": "AniList", "version": None}

    async def search(self, title: str, author: str | None = None, *, limit: int = 8
                     ) -> list[ProviderMatch]:
        q = ("query($q:String,$n:Int){Page(perPage:$n){media(search:$q,type:MANGA,"
             "sort:SEARCH_MATCH){id format title{romaji english native} "
             "coverImage{large} startDate{year} siteUrl}}}")
        data = await self._gql(q, {"q": title, "n": limit})
        out: list[ProviderMatch] = []
        for m in ((data.get("Page") or {}).get("media") or []):
            if not m.get("id"):
                continue
            out.append(ProviderMatch(
                ref=str(m["id"]),
                title=_anilist_title(m.get("title")),
                year=(m.get("startDate") or {}).get("year"),
                cover_url=(m.get("coverImage") or {}).get("large"),
                media_kind=_anilist_media_kind(m.get("format")),
                url=m.get("siteUrl"),
            ))
        return out

    @staticmethod
    def _meta_from_media(m: dict) -> ProviderMeta | None:
        """Build a ProviderMeta from one AniList Media node (shared by fetch + fetch_many)."""
        if not isinstance(m, dict) or not m.get("id"):
            return None
        chapters = m.get("chapters")
        related = []
        for e in ((m.get("relations") or {}).get("edges") or []):
            node = e.get("node") or {}
            # Only relate other readable works (manga/novels), not anime adaptations.
            if (node.get("type") or "").upper() != "MANGA" or not node.get("title"):
                continue
            related.append(RelatedWork(
                title=_anilist_title(node.get("title")),
                relation=_ANILIST_REL.get((e.get("relationType") or "").upper(), "related"),
                ref=str(node["id"]) if node.get("id") else None,
            ))
        return ProviderMeta(
            ref=str(m["id"]),
            title=_anilist_title(m.get("title")),
            author=_anilist_author(m.get("staff")),
            synopsis=re.sub(r"<[^>]+>", " ", m.get("description") or "").strip() or None,
            cover_url=(m.get("coverImage") or {}).get("extraLarge")
            or (m.get("coverImage") or {}).get("large"),
            media_kind=_anilist_media_kind(m.get("format")),
            total_units=int(chapters) if isinstance(chapters, int) and chapters > 0 else None,
            unit_kind="chapters",  # the whole point: an authoritative chapter count
            status="complete" if (m.get("status") or "").upper() == "FINISHED" else "ongoing",
            # Marker advances when the chapter count grows or the series finishes.
            release_marker=f"{chapters or 0}:{(m.get('status') or '').upper()}",
            related=related,
            genres=[g for g in (m.get("genres") or []) if g],
            tags=_anilist_tags(m.get("tags")),
            popularity=m.get("popularity") if isinstance(m.get("popularity"), int) else None,
            is_adult=bool(m.get("isAdult")),
            url=m.get("siteUrl"),
            year=(m.get("startDate") or {}).get("year"),
            # AniList scores 0–100 → the 0–10 convention.
            rating=round(m["averageScore"] / 10.0, 2)
            if isinstance(m.get("averageScore"), (int, float)) else None,
            extra={"anilist_id": m["id"], "format": m.get("format"), "volumes": m.get("volumes"),
                   "average_score": m.get("averageScore")},
        )

    async def fetch(self, ref: str) -> ProviderMeta | None:
        q = ("query($id:Int){Media(id:$id,type:MANGA){" + _ANILIST_FIELDS +
             "relations{edges{relationType node{id type title{romaji english native}}}}}}")
        try:
            data = await self._gql(q, {"id": int(ref)})
        except (TypeError, ValueError):
            return None
        return self._meta_from_media(data.get("Media"))

    async def fetch_many(self, refs: list[str]) -> dict[str, ProviderMeta | None]:
        """Batch by-id fetch via Page(media:{id_in:[…]}) ~50/request — cuts the release-watch from
        N GraphQL round-trips to ~N/50 (14B). Non-integer/unknown refs map to None."""
        out: dict[str, ProviderMeta | None] = {ref: None for ref in refs}
        ids: list[int] = []
        for ref in refs:
            try:
                ids.append(int(ref))
            except (TypeError, ValueError):
                continue
        q = ("query($ids:[Int],$n:Int){Page(perPage:$n){media(id_in:$ids,type:MANGA){"
             + _ANILIST_FIELDS
             + "relations{edges{relationType node{id type title{romaji english native}}}}}}}")
        for i in range(0, len(ids), 50):
            page_ids = ids[i:i + 50]
            data = await self._gql(q, {"ids": page_ids, "n": len(page_ids)})
            for m in ((data.get("Page") or {}).get("media") or []):
                meta = self._meta_from_media(m)
                if meta is not None:
                    out[str(m["id"])] = meta
        return out


# --------------------------------------------------------------------- novelupdates
NOVELUPDATES_URL = "https://www.novelupdates.com"
_NU_CHALLENGE = ("just a moment", "challenge-platform", "verifying you are human",
                 "checking your browser", "enable javascript and cookies")
_NU_DEFAULT_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0 Safari/537.36")


def _nu_slug(href: str) -> str | None:
    m = re.search(r"/series/([^/?#]+)", href or "")
    return m.group(1) if m else None


def _nu_parse_search(html: str, base: str) -> list[ProviderMatch]:
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html or "", "lxml")
    out: list[ProviderMatch] = []
    seen: set[str] = set()
    for a in soup.select(".search_title a[href], .search_body_nu .search_title a[href]"):
        href = a.get("href", "")
        slug = _nu_slug(href)
        title = a.get_text(strip=True)
        if not slug or not title or slug in seen:
            continue
        seen.add(slug)
        out.append(ProviderMatch(ref=slug, title=title, media_kind="text",
                                 url=href if href.startswith("http") else f"{base}/series/{slug}/"))
    return out


def _nu_parse_series(html: str, ref: str, url: str) -> ProviderMeta | None:
    """Parse a NovelUpdates series page → ProviderMeta with an authoritative CHAPTER count.
    Pure function (no I/O) so the DOM logic is unit-testable without clearing Cloudflare."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html or "", "lxml")
    title_el = soup.select_one(".seriestitlenu")
    if not title_el or not title_el.get_text(strip=True):
        return None
    title = title_el.get_text(strip=True)
    author = None
    a_el = soup.select_one("#showauthors a, #authtag")
    if a_el:
        author = a_el.get_text(strip=True) or None
    # COO status block, e.g. "2334 Chapters (Completed)" / "248 Chapters (Ongoing)". The leading
    # number is the source-language chapter total — the authoritative count we validate against.
    status_el = soup.select_one("#editstatus")
    status_txt = status_el.get_text(" ", strip=True) if status_el else ""
    cm = re.search(r"([\d,]+)\s*chapters?", status_txt, re.I)
    chapters = int(cm.group(1).replace(",", "")) if cm else None
    complete = "complet" in status_txt.lower()  # 'Completed' / 'Complete'
    desc_el = soup.select_one("#editdescription")
    synopsis = desc_el.get_text("\n", strip=True) if desc_el else None
    cover_el = soup.select_one(".seriesimg img, .wpb_wrapper img")
    cover = (cover_el.get("src") if cover_el else None) or None
    return ProviderMeta(
        ref=ref, title=title, author=author, synopsis=synopsis or None, cover_url=cover,
        media_kind="text", total_units=chapters, unit_kind="chapters",
        status="complete" if complete else "ongoing",
        # Marker advances when the chapter total grows or the status flips to completed.
        release_marker=f"{chapters or 0}:{'c' if complete else 'o'}",
        url=url, extra={"slug": ref, "status_text": status_txt[:80]},
    )


class NovelUpdatesProvider(MetadataProvider):
    """NovelUpdates (novelupdates.com) — the canonical chapter-count authority for translated web
    novels (Chinese/Korean/Japanese), the source ranobedb/AniList don't cover well. It reports a
    real CHAPTER total, so it can validate a web-novel's chapter count and surface when more exist.

    NovelUpdates is behind a Cloudflare *managed* challenge that a headless browser usually can't
    clear on its own. To use it, paste a ``cf_clearance`` cookie (and the matching ``user_agent``)
    from a logged-in browser session into the integration config; without one this provider raises
    a clear error rather than silently returning no matches."""

    kind = "novelupdates"
    timeout = 25.0
    tracks_releases = True

    def __init__(self, base_url: str = "", api_key: str = "", config: dict | None = None) -> None:
        super().__init__(base_url or NOVELUPDATES_URL, api_key, config)
        # With an operator cf_clearance cookie we use a fast plain-HTTP fetch; without one we fall
        # back to a slow headless render — flag that so the on-hook path can skip us.
        self.renders = not bool(str((config or {}).get("cf_clearance") or "").strip())

    async def _html(self, url: str) -> str:
        """Fetch a NovelUpdates page, preferring an operator-supplied cf_clearance cookie and
        falling back to the shared headless-render path. Raises if the challenge isn't cleared."""
        cf = str(self.config.get("cf_clearance") or "").strip()
        if cf:
            ua = str(self.config.get("user_agent") or "").strip() or _NU_DEFAULT_UA
            r = await self._get(url, cookies={"cf_clearance": cf}, headers={"User-Agent": ua})
            if r.status_code != 200:
                raise IntegrationError(
                    f"novelupdates HTTP {r.status_code} — the cf_clearance cookie may be stale "
                    "(it's tied to your IP + User-Agent; refresh it from your browser).")
            html = r.text
        else:
            from ..ingestion.engine import get_fetcher
            # One attempt only: a managed challenge won't clear on retry, and this can run on the
            # hot hook path — fail fast rather than burn 3× the render timeout.
            page = await get_fetcher().get_html(self.kind, url, force_render=True, max_retries=1)
            if getattr(page, "status_code", 0) >= 400:
                raise IntegrationError(f"novelupdates render returned HTTP {page.status_code}")
            html = page.text or ""
        if any(mk in html[:5000].lower() for mk in _NU_CHALLENGE):
            raise IntegrationError(
                "novelupdates is behind a Cloudflare challenge that couldn't be cleared — add a "
                "'cf_clearance' cookie (+ matching 'user_agent') to the integration config.")
        return html

    async def test_connection(self) -> dict:
        html = await self._html(f"{self.base_url}/?s=bookworm")
        return {"ok": True, "app": "NovelUpdates", "version": None,
                "detail": f"{len(_nu_parse_search(html, self.base_url))} search hits"}

    async def search(self, title: str, author: str | None = None, *, limit: int = 8
                     ) -> list[ProviderMatch]:
        from urllib.parse import quote_plus
        html = await self._html(f"{self.base_url}/?s={quote_plus(title)}&post_type=seriesplans")
        return _nu_parse_search(html, self.base_url)[:limit]

    async def fetch(self, ref: str) -> ProviderMeta | None:
        url = f"{self.base_url}/series/{ref}/"
        return _nu_parse_series(await self._html(url), ref, url)


# --------------------------------------------------------------------- hardcover
HARDCOVER_API = "https://api.hardcover.app/v1/graphql"
# Search returns a Typesense payload as a JSON scalar; we read hits[].document.
_HC_SEARCH_Q = (
    'query($q:String!,$n:Int!){ search(query:$q, query_type:"Book", per_page:$n, page:1)'
    "{ results } }"
)


def _hc_norm_token(token: str | None) -> str:
    """Hardcover's settings page sometimes shows the token already prefixed with 'Bearer '."""
    t = (token or "").strip()
    return t[7:].strip() if t.lower().startswith("bearer ") else t


def _hc_hits(data: dict) -> list[dict]:
    """The list of result documents from a Hardcover search payload (JSON scalar → dict or str)."""
    import json as _json
    res = (data.get("search") or {}).get("results") if isinstance(data, dict) else None
    if isinstance(res, str):
        try:
            res = _json.loads(res)
        except ValueError:
            return []
    if not isinstance(res, dict):
        return []
    out: list[dict] = []
    for h in res.get("hits") or []:
        doc = h.get("document") if isinstance(h, dict) else None
        if isinstance(doc, dict):
            out.append(doc)
    return out


def _hc_image(doc: dict) -> str | None:
    img = doc.get("image")
    if isinstance(img, dict):
        return img.get("url")
    if isinstance(img, str) and img:
        return img
    return doc.get("cover_image_url") or None


def _hc_authors(doc: dict) -> str | None:
    out: list[str] = []
    for n in doc.get("author_names") or []:
        if isinstance(n, str) and n.strip():
            out.append(n.strip())
    if not out:  # fall back to structured contributions (list of {author:{name}})
        for c in doc.get("contributions") or []:
            name = ((c.get("author") or {}) if isinstance(c, dict) else {}).get("name")
            if isinstance(name, str) and name.strip():
                out.append(name.strip())
    return ", ".join(dict.fromkeys(out)) or None


# Manga/comic genre signals in a Hardcover search document. (NB: "light novel" is PROSE — excluded.)
_HC_COMIC_RE = re.compile(r"manga|manhwa|manhua|graphic novel|\bcomics?\b|webtoon", re.I)


def _hc_media_kind(doc: dict) -> str:
    """'comic' if the Hardcover doc's genre/tag/taxonomy signals manga/comics, else 'text'.
    Defensive: Hardcover's search document is an opaque blob, so probe the genre-like fields it's
    known to carry; if none are present (or none match), fall back to the safe 'text' default — so a
    Hardcover manga match can merge with comic-source rows instead of being bucket-split as prose."""
    blobs: list[str] = []
    for key in ("genres", "moods", "tags", "cached_tags"):
        v = doc.get(key)
        if isinstance(v, list):
            blobs.extend(str(x) for x in v)
        elif isinstance(v, str):
            blobs.append(v)
    for t in doc.get("taxonomies") or []:  # list of {tag, category}
        if isinstance(t, dict):
            blobs.append(str(t.get("tag") or ""))
    return "comic" if _HC_COMIC_RE.search(" ".join(blobs)) else "text"


class HardcoverProvider(MetadataProvider):
    """Hardcover.app — a community books database (a Goodreads alternative) with strong coverage of
    titles Google Books / Open Library miss. GraphQL API; requires a personal Bearer token from the
    user's Hardcover account settings (rate-limited 60 req/min). Used for discovery/resolution and
    canonical author / cover."""

    kind = "hardcover"

    def __init__(self, base_url: str = "", api_key: str = "", config: dict | None = None) -> None:
        super().__init__(base_url or HARDCOVER_API, api_key, config)
        if not self.base_url:
            self.base_url = HARDCOVER_API

    async def _graphql(self, query: str, variables: dict | None = None) -> dict:
        tok = _hc_norm_token(self.api_key)
        if not tok:
            raise IntegrationError("hardcover: no API token configured (get one in account settings)")
        r = await self._post(self.base_url, json={"query": query, "variables": variables or {}},
                             headers={"Authorization": f"Bearer {tok}"})
        if r.status_code != 200:
            raise IntegrationError(f"hardcover HTTP {r.status_code}: {r.text[:200]}")
        data = r.json() or {}
        if data.get("errors"):
            msg = (data["errors"][0] or {}).get("message", "query failed")
            raise IntegrationError(f"hardcover: {msg}")
        return data.get("data") or {}

    async def test_connection(self) -> dict:
        await self._graphql(_HC_SEARCH_Q, {"q": "dune", "n": 1})
        return {"ok": True, "app": "Hardcover", "version": None}

    async def search(self, title: str, author: str | None = None, *, limit: int = 8
                     ) -> list[ProviderMatch]:
        q = f"{title} {author}".strip() if author else title
        data = await self._graphql(_HC_SEARCH_Q, {"q": q, "n": min(25, max(1, limit))})
        out: list[ProviderMatch] = []
        for doc in _hc_hits(data):
            title_v = doc.get("title")
            ref = str(doc.get("id") or doc.get("slug") or "")
            if not title_v or not ref:
                continue
            slug = doc.get("slug")
            out.append(ProviderMatch(
                ref=ref, title=title_v, author=_hc_authors(doc),
                year=doc.get("release_year"), cover_url=_hc_image(doc),
                synopsis=(doc.get("description") or "").strip() or None,
                media_kind=_hc_media_kind(doc),
                url=f"https://hardcover.app/books/{slug}" if slug else None,
            ))
        return out[:limit]


_PROVIDERS = {"ranobedb": RanobeDbProvider, "goodreads": GoodreadsProvider,
              "googlebooks": GoogleBooksProvider, "anilist": AniListProvider,
              "novelupdates": NovelUpdatesProvider, "hardcover": HardcoverProvider}
METADATA_KINDS = tuple(_PROVIDERS)


def is_metadata_kind(kind: str) -> bool:
    return kind in _PROVIDERS


def provider_for(integration, config: dict | None = None) -> MetadataProvider:
    """Build a provider for an integration. ``config`` overrides the stored provider config
    (used to read a different Goodreads shelf than the connection's default)."""
    cls = _PROVIDERS.get(integration.kind)
    if cls is None:
        raise IntegrationError(f"{integration.kind!r} is not a metadata provider")
    cfg = config if config is not None else (integration.config or {})
    return cls(integration.base_url or "", integration.api_key or "", cfg)
