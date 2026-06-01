"""Kapowarr client — comics / manga (Kapowarr API).

Kapowarr authenticates with an ``api_key`` query parameter and wraps responses in
``{"error": ..., "result": ...}``.
"""
from __future__ import annotations

from .base import BaseClient, ExternalWork, IntegrationError, RootFolder, strip_html

API = "/api"


class KapowarrClient(BaseClient):
    provider = "kapowarr"

    def _params(self, extra: dict | None = None) -> dict:
        p = {"api_key": self.api_key}
        if extra:
            p.update(extra)
        return p

    @staticmethod
    def _unwrap(data):
        if isinstance(data, dict):
            if data.get("error"):
                raise IntegrationError(f"kapowarr: {data['error']}")
            if "result" in data:
                return data["result"]
        return data

    async def _result(self, path: str, params: dict | None = None):
        return self._unwrap(await self._get(f"{API}{path}", params=self._params(params)))

    async def _post_result(self, path: str, body: dict, params: dict | None = None):
        return self._unwrap(
            await self._post(f"{API}{path}", params=self._params(params), json=body)
        )

    async def test_connection(self) -> dict:
        try:
            data = await self._result("/system/about")
            ver = (data or {}).get("version") if isinstance(data, dict) else None
            return {"app": "Kapowarr", "version": ver}
        except IntegrationError:
            # Older builds may lack /system/about — a volumes ping still proves auth.
            await self._result("/volumes")
            return {"app": "Kapowarr", "version": None}

    async def root_folders(self) -> list[RootFolder]:
        data = await self._result("/rootfolder")
        out = []
        for rf in data or []:
            path = rf.get("folder") or rf.get("path")
            if path:
                out.append(RootFolder(id=rf.get("id"), path=path))
        return out

    async def list_library(self) -> list[ExternalWork]:
        data = await self._result("/volumes")
        return [self._to_ext(v, in_library=True) for v in (data or [])]

    async def lookup(self, term: str) -> list[ExternalWork]:
        data = await self._result("/volumes/search", {"query": term})
        return [self._to_ext(v, in_library=False) for v in (data or [])]

    async def _root_folder_id(self, root_folder: str | None) -> int | None:
        data = await self._result("/rootfolder")
        for rf in data or []:
            path = rf.get("folder") or rf.get("path")
            if root_folder and path == root_folder:
                return rf.get("id")
        return data[0].get("id") if data else None

    async def grab(
        self, extra: dict, *, root_folder: str | None = None,
        quality_profile_id: int | None = None, metadata_profile_id: int | None = None,
    ) -> dict:
        """Add the volume (by ComicVine id) to Kapowarr + search for issues. Downloads
        land in the root folder where Shelf's watched-folder ingestion finds them."""
        cv = extra.get("comicvine_id") or extra.get("provider_ref")
        if not cv:
            raise IntegrationError("kapowarr: this title has no comicvine_id to add")
        rid = await self._root_folder_id(root_folder)
        if rid is None:
            raise IntegrationError("kapowarr: no root folder configured")
        body = {
            "comicvine_id": int(cv) if str(cv).isdigit() else cv,
            "root_folder_id": rid, "monitor": True, "monitoring_scheme": "all",
        }
        resp = await self._post_result("/volumes", body)
        vol_id = resp.get("id") if isinstance(resp, dict) else None
        if vol_id:  # best-effort search trigger (monitored volumes also auto-search)
            try:
                await self._post_result(f"/volumes/{vol_id}/search", {})
            except IntegrationError:
                pass
        return {"id": vol_id, "status": "added", "searching": True}

    # ----------------------------------------------------------------------
    def _to_ext(self, v: dict, *, in_library: bool) -> ExternalWork:
        ref = str(v.get("comicvine_id") or v.get("id") or "")
        # Only keep an absolute cover URL (never embed our api_key in a stored URL).
        cover = v.get("cover_link") or v.get("cover")
        if cover and not str(cover).startswith("http"):
            cover = None
        year = v.get("year")
        try:
            year = int(year) if year else None
        except (TypeError, ValueError):
            year = None
        return ExternalWork(
            provider="kapowarr",
            ref=ref,
            title=v.get("title") or "Untitled",
            author=v.get("publisher"),
            year=year,
            overview=strip_html(v.get("description") or v.get("summary")),
            cover_url=cover,
            media_kind="comic",
            in_library=in_library,
            downloaded=bool(v.get("issues_downloaded") or v.get("issues_downloaded_monitored")),
            url=v.get("comicvine_info") or None,
            extra={"comicvine_id": v.get("comicvine_id"), "kapowarrId": v.get("id")},
        )
