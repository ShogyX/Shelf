"""Audiobookshelf (ABS) client.

ABS is a self-hosted audiobook/ebook LIBRARY SERVER — no acquisition, no wishlist. It scans library
FOLDERS on disk and auto-ingests them, so Shelf "pushes" a title simply by placing the file in a
folder ABS watches (typically Shelf's own stock/audiobook dirs on a shared mount). This client is
used to: verify the connection, discover the watched folders, optionally nudge a scan + match
metadata, and — for the wanted-pull — read library items so Shelf can detect an item present in only
ONE format (ebook XOR audiobook) and fetch the missing half.

Auth: an ABS API key (Settings → Users → API Keys), sent as ``Authorization: Bearer <key>``.
"""
from __future__ import annotations

from .. import telemetry
from .base import BaseClient, IntegrationError, RootFolder

_MAX_PAGES = 1000  # safety bound (200/page → up to 200k items) against a misbehaving server


class AudiobookshelfClient(BaseClient):
    provider = "audiobookshelf"

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}", "accept": "application/json"}

    async def test_connection(self) -> dict:
        me = await self._get("/api/me", headers=self._headers())
        user = (me or {}).get("username") if isinstance(me, dict) else None
        return {"app": "Audiobookshelf", "version": None,
                "detail": f"user {user}" if user else None}

    async def book_libraries(self) -> list[dict]:
        """The ABS book libraries (id + name + on-disk folder paths)."""
        data = await self._get("/api/libraries", headers=self._headers())
        out = []
        for lib in (data or {}).get("libraries", []) if isinstance(data, dict) else []:
            if lib.get("mediaType") != "book":
                continue
            out.append({"id": lib.get("id"), "name": lib.get("name"),
                        "folders": [f.get("fullPath") for f in (lib.get("folders") or [])
                                    if f.get("fullPath")]})
        return out

    async def root_folders(self) -> list[RootFolder]:
        """Every watched folder across ABS book libraries — surfaced by the Test button so the
        operator can confirm ABS is pointed at Shelf's stock/audiobook paths."""
        out: list[RootFolder] = []
        for lib in await self.book_libraries():
            for path in lib["folders"]:
                out.append(RootFolder(id=None, path=path))
        return out

    async def scan(self, library_id: str) -> None:
        """Nudge ABS to rescan a library's folders (it also auto-detects via a file watcher). The scan
        endpoint replies ``200 OK`` as plain text, so this checks status only (the JSON client would
        choke on the non-JSON body)."""
        url = f"{self.base_url}/api/libraries/{library_id}/scan"
        try:
            async with telemetry.instrument("integration", timeout=self._timeout,
                                             follow_redirects=True) as c:
                r = await c.post(url, headers=self._headers())
        except Exception as exc:  # noqa: BLE001
            raise IntegrationError(f"audiobookshelf: cannot reach {self.base_url} ({exc})") from exc
        if r.status_code >= 400:
            raise IntegrationError(f"audiobookshelf: scan HTTP {r.status_code}")

    async def iter_items(self, library_id: str, *, page_limit: int = 200) -> list[dict]:
        """All items in a book library, normalized to the fields Shelf needs for the wanted-pull:
        id, title, author, isbn, asin, has_ebook, has_audio. Paged."""
        out: list[dict] = []
        for page in range(_MAX_PAGES):  # bounded so a misbehaving server can't loop forever
            data = await self._get(
                f"/api/libraries/{library_id}/items", headers=self._headers(),
                params={"limit": page_limit, "page": page, "minified": 1})
            results = (data or {}).get("results", []) if isinstance(data, dict) else []
            for it in results:
                media = it.get("media", {}) or {}
                meta = media.get("metadata", {}) or {}
                out.append({
                    "id": it.get("id"),
                    "title": meta.get("title"),
                    "author": meta.get("authorName") or meta.get("author"),
                    "isbn": meta.get("isbn"),
                    "asin": meta.get("asin"),
                    # The minified list exposes ebookFormat (truthy = has an ebook) + numAudioFiles,
                    # NOT ebookFile/audioFiles (those are only in the full item detail).
                    "ebook_format": media.get("ebookFormat"),
                    "has_ebook": bool(media.get("ebookFile") or media.get("ebookFormat")),
                    "has_audio": bool(media.get("numAudioFiles") or media.get("audioFiles")),
                })
            if len(results) < page_limit:
                break
        return out

    async def match_item(self, item_id: str, *, title: str | None = None,
                         author: str | None = None, isbn: str | None = None,
                         asin: str | None = None, provider: str = "google") -> None:
        """Ask ABS to match an item against a metadata provider (best-effort metadata enrichment)."""
        body: dict = {"provider": provider}
        for k, v in (("title", title), ("author", author), ("isbn", isbn), ("asin", asin)):
            if v:
                body[k] = v
        await self._post(f"/api/items/{item_id}/match", headers=self._headers(), json=body)
