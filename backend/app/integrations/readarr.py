"""Readarr client — books / novels (Readarr API v1)."""
from __future__ import annotations

from .base import BaseClient, ExternalWork, IntegrationError, RootFolder, strip_html

API = "/api/v1"


class ReadarrClient(BaseClient):
    provider = "readarr"

    def _headers(self) -> dict:
        return {"X-Api-Key": self.api_key}

    async def test_connection(self) -> dict:
        data = await self._get(f"{API}/system/status", headers=self._headers())
        return {"app": data.get("appName", "Readarr"), "version": data.get("version")}

    async def root_folders(self) -> list[RootFolder]:
        data = await self._get(f"{API}/rootfolder", headers=self._headers())
        out = []
        for rf in data or []:
            path = rf.get("path")
            if path:
                out.append(RootFolder(id=rf.get("id"), path=path))
        return out

    async def list_library(self) -> list[ExternalWork]:
        data = await self._get(f"{API}/book", headers=self._headers())
        return [self._to_ext(b, in_library=True) for b in (data or [])]

    async def lookup(self, term: str) -> list[ExternalWork]:
        data = await self._get(f"{API}/book/lookup", headers=self._headers(), params={"term": term})
        return [self._to_ext(b, in_library=False) for b in (data or [])]

    async def _first_id(self, path: str) -> int | None:
        data = await self._get(path, headers=self._headers())
        return data[0].get("id") if data else None

    async def grab(
        self, extra: dict, *, root_folder: str | None = None,
        quality_profile_id: int | None = None, metadata_profile_id: int | None = None,
    ) -> dict:
        """Add the book (and its author) to Readarr and search for it. The downloaded
        file lands in the root folder, where Shelf's watched-folder ingestion finds it."""
        fid = str(extra.get("foreignBookId") or "")
        if not fid:
            raise IntegrationError("readarr: this title has no foreignBookId to add")
        results = await self._get(
            f"{API}/book/lookup", headers=self._headers(), params={"term": fid}
        )
        book = next((b for b in results or [] if str(b.get("foreignBookId")) == fid), None)
        book = book or (results[0] if results else None)
        if not book:
            raise IntegrationError("readarr: title not found via lookup")

        qp = quality_profile_id or await self._first_id(f"{API}/qualityprofile")
        mp = metadata_profile_id or await self._first_id(f"{API}/metadataprofile")
        root = root_folder
        if not root:
            rfs = await self.root_folders()
            root = rfs[0].path if rfs else None
        if not (qp and mp and root):
            raise IntegrationError(
                "readarr: need a quality profile, metadata profile and root folder set up"
            )

        author = dict(book.get("author") or {})
        author.update({
            "qualityProfileId": qp, "metadataProfileId": mp, "rootFolderPath": root,
            "monitored": True, "addOptions": {"searchForMissingBooks": False},
        })
        payload = {
            **book, "author": author, "monitored": True,
            "addOptions": {"searchForNewBook": True},
        }
        resp = await self._post(f"{API}/book", headers=self._headers(), json=payload)
        return {"id": resp.get("id"), "status": "added", "searching": True}

    # ----------------------------------------------------------------------
    def _to_ext(self, b: dict, *, in_library: bool) -> ExternalWork:
        author = (b.get("author") or {}).get("authorName") or b.get("authorTitle")
        stats = b.get("statistics") or {}
        ref = str(b.get("foreignBookId") or b.get("id") or "")
        rd = b.get("releaseDate") or ""
        year = int(rd[:4]) if rd[:4].isdigit() else None
        slug = b.get("titleSlug")
        return ExternalWork(
            provider="readarr",
            ref=ref,
            title=b.get("title") or "Untitled",
            author=author,
            year=year,
            overview=strip_html(b.get("overview")),
            cover_url=self._cover(b.get("images")),
            media_kind="text",
            in_library=in_library,
            downloaded=bool(stats.get("bookFileCount", 0)),
            url=f"{self.base_url}/book/{slug}" if slug else None,
            extra={
                "foreignBookId": b.get("foreignBookId"),
                "readarrId": b.get("id"),
                "titleSlug": slug,
            },
        )

    def _cover(self, images: list | None) -> str | None:
        # Prefer the absolute remoteUrl (public image host) over the local /MediaCover
        # path (which can require the API key) so the browser can load it.
        ordered = sorted(images or [], key=lambda i: 0 if i.get("coverType") == "cover" else 1)
        for img in ordered:
            url = img.get("remoteUrl")
            if url and url.startswith("http"):
                return url
        for img in ordered:
            url = img.get("url")
            if url:
                return url if url.startswith("http") else f"{self.base_url}{url}"
        return None
