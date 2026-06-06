"""Prowlarr client — indexer search aggregator (Prowlarr API v1).

Prowlarr is not a library to sync; it's a *search* provider. Given a query it returns
candidate releases (across its configured indexers) that the matching engine scores and
the orchestrator hands to a downloader (SABnzbd). We deliberately only surface usenet
releases by default (Shelf's download path is SABnzbd).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .base import BaseClient, IntegrationError, RootFolder

API = "/api/v1"

# Newznab/Torznab category ids relevant to books. 7000=Books, 7020=Books/EBook,
# 7060=Books/Comics, 3030=Audio/Audiobook. Defaults target ebooks; audiobooks are opt-in.
CAT_EBOOK = 7020
CAT_BOOKS = 7000
CAT_COMICS = 7060
CAT_AUDIOBOOK = 3030
DEFAULT_EBOOK_CATEGORIES = [7000, 7020]
DEFAULT_AUDIOBOOK_CATEGORIES = [3030]


@dataclass
class Release:
    """One candidate release returned by a Prowlarr search."""

    title: str
    download_url: str | None          # NZB/torrent URL to hand to the downloader
    protocol: str                     # "usenet" | "torrent"
    indexer: str | None = None
    indexer_id: int | None = None
    size: int = 0                     # bytes
    categories: list[int] = field(default_factory=list)
    category_names: list[str] = field(default_factory=list)
    info_url: str | None = None
    guid: str | None = None
    publish_date: str | None = None
    age_days: float | None = None
    grabs: int | None = None
    seeders: int | None = None        # torrent only
    raw: dict = field(default_factory=dict)

    @property
    def size_mb(self) -> float:
        return round((self.size or 0) / 1_000_000, 2)


def _to_release(r: dict) -> Release:
    cats = r.get("categories") or []
    return Release(
        title=r.get("title") or "",
        download_url=r.get("downloadUrl") or r.get("magnetUrl"),
        protocol=(r.get("protocol") or "").lower() or "unknown",
        indexer=r.get("indexer"),
        indexer_id=r.get("indexerId"),
        size=int(r.get("size") or 0),
        categories=[c.get("id") for c in cats if c.get("id") is not None],
        category_names=[c.get("name") for c in cats if c.get("name")],
        info_url=r.get("infoUrl"),
        guid=r.get("guid"),
        publish_date=r.get("publishDate"),
        age_days=r.get("age"),
        grabs=r.get("grabs"),
        seeders=(r.get("seeders") if r.get("seeders") is not None else None),
        raw=r,
    )


class ProwlarrClient(BaseClient):
    provider = "prowlarr"

    def _headers(self) -> dict:
        return {"X-Api-Key": self.api_key}

    async def test_connection(self) -> dict:
        data = await self._get(f"{API}/system/status", headers=self._headers())
        detail = None
        try:
            idx = await self.indexers()
            enabled = [i for i in idx if i.get("enable")]
            usenet = [i for i in enabled if i.get("protocol") == "usenet"]
            detail = f"{len(enabled)} indexer(s) enabled · {len(usenet)} usenet"
        except IntegrationError:
            pass
        return {
            "app": data.get("appName", "Prowlarr"),
            "version": data.get("version"),
            "detail": detail,
        }

    async def root_folders(self) -> list[RootFolder]:
        # Prowlarr is a search source, not a download target — it has no root folders.
        return []

    async def indexers(self) -> list[dict]:
        """List configured indexers (id, name, protocol, enabled, supported categories)."""
        data = await self._get(f"{API}/indexer", headers=self._headers())
        out: list[dict] = []
        for i in data or []:
            caps = ((i.get("capabilities") or {}).get("categories")) or []
            out.append({
                "id": i.get("id"),
                "name": i.get("name"),
                "protocol": i.get("protocol"),
                "enable": bool(i.get("enable")),
                "categories": [
                    {"id": c.get("id"), "name": c.get("name")} for c in caps
                ],
            })
        return out

    async def search(
        self,
        query: str,
        *,
        categories: list[int] | None = None,
        indexer_ids: list[int] | None = None,
        protocols: tuple[str, ...] = ("usenet",),
        limit: int = 100,
        offset: int = 0,
    ) -> list[Release]:
        """Search indexers for `query`. Results are filtered to `protocols` (usenet by
        default) since Shelf downloads via SABnzbd. Returns parsed Release objects."""
        params: dict = {"query": query, "type": "search", "limit": limit, "offset": offset}
        if categories:
            params["categories"] = list(categories)
        if indexer_ids:
            params["indexerIds"] = list(indexer_ids)
        data = await self._get(f"{API}/search", headers=self._headers(), params=params)
        releases = [_to_release(r) for r in (data or [])]
        if protocols:
            allowed = {p.lower() for p in protocols}
            releases = [r for r in releases if r.protocol in allowed]
        return releases
