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

from . import IntegrationError

RANOBEDB_API = "https://ranobedb.org/api/v0"
RANOBEDB_IMG = "https://images.ranobedb.org"


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
    url: str | None = None
    extra: dict = field(default_factory=dict)


class MetadataProvider:
    kind = "abstract"
    timeout = 20.0

    def __init__(self, base_url: str = "", api_key: str = "", config: dict | None = None) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.config = config or {}

    async def _get(self, url: str, **kw):
        import asyncio

        from ..ingestion.netguard import BlockedAddress, assert_public_url
        # SSRF guard: the base URL / Goodreads user id are operator-configurable. Block
        # internal/metadata targets (DNS resolved off the event loop).
        try:
            await asyncio.to_thread(assert_public_url, url)
        except BlockedAddress as exc:
            raise IntegrationError(f"{self.kind}: refusing to fetch {url}: {exc}") from exc
        headers = {"User-Agent": "Mozilla/5.0 (compatible; ShelfReader/0.1)",
                   "Accept": "application/json, */*"}
        try:
            async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as c:
                return await c.get(url, headers=headers, **kw)
        except httpx.HTTPError as exc:
            raise IntegrationError(f"{self.kind}: request to {url} failed: {exc}") from exc

    async def test_connection(self) -> dict:  # pragma: no cover - interface
        raise NotImplementedError

    async def search(self, title: str, author: str | None = None, *, limit: int = 8
                     ) -> list[ProviderMatch]:
        return []

    async def fetch(self, ref: str) -> ProviderMeta | None:
        return None

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
        if r.status_code != 200:
            return []
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
        if r.status_code != 200:
            return None
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


_PROVIDERS = {"ranobedb": RanobeDbProvider, "goodreads": GoodreadsProvider}
METADATA_KINDS = tuple(_PROVIDERS)


def is_metadata_kind(kind: str) -> bool:
    return kind in _PROVIDERS


def provider_for(integration) -> MetadataProvider:
    cls = _PROVIDERS.get(integration.kind)
    if cls is None:
        raise IntegrationError(f"{integration.kind!r} is not a metadata provider")
    return cls(integration.base_url or "", integration.api_key or "", integration.config or {})
