"""Storyteller client (REST API v2).

Storyteller (smoores-dev/storyteller) synchronizes an EPUB with its audiobook into a read-along book.
It ingests files BY SERVER-LOCAL PATH (``POST /api/v2/books {paths}``) and writes metadata back into
those files — so Shelf pushes COPIES (never its originals). Text input must be EPUB (Storyteller does
NOT convert), so Shelf converts ebook→EPUB on demand before pushing. Each book exposes nullable
``ebook`` / ``audiobook`` / ``readaloud`` relations: a book missing one of ebook/audiobook is the
"wanted" signal Shelf acts on.

Auth: ``POST /api/v2/token`` (multipart form: usernameOrEmail, password) → a bearer token.
"""
from __future__ import annotations

from .. import telemetry
from .base import BaseClient, IntegrationError, RootFolder

API = "/api/v2"


class StorytellerClient(BaseClient):
    provider = "storyteller"

    def __init__(self, base_url: str, api_key: str, *, kind: str | None = None,
                 config: dict | None = None) -> None:
        super().__init__(base_url, api_key, kind=kind or "storyteller", config=config)
        # base_url + username (config) + password (api_key, write-only) → a minted bearer token.
        self._username = ((config or {}).get("username") or "").strip()
        self._token: str | None = None

    async def _mint_token(self) -> str:
        """Exchange username/password for a bearer token (multipart form, not JSON)."""
        url = f"{self.base_url}{API}/token"
        try:
            async with telemetry.instrument("integration", timeout=self._timeout,
                                             follow_redirects=True) as c:
                r = await c.post(url, data={"usernameOrEmail": self._username,
                                            "password": self.api_key})
        except Exception as exc:  # noqa: BLE001
            raise IntegrationError(f"storyteller: cannot reach {self.base_url} ({exc})") from exc
        if r.status_code in (401, 403):
            raise IntegrationError("storyteller: unauthorized — check the username/password")
        if r.status_code >= 400:
            raise IntegrationError(f"storyteller: HTTP {r.status_code} minting token")
        tok = (r.json() or {}).get("access_token") if r.content else None
        if not tok:
            raise IntegrationError("storyteller: no access_token in token response")
        return tok

    async def _auth(self) -> dict:
        if self._token is None:
            self._token = await self._mint_token()
        return {"Authorization": f"Bearer {self._token}", "accept": "application/json"}

    async def test_connection(self) -> dict:
        await self._auth()  # minting the token IS the credential check
        return {"app": "Storyteller", "version": None,
                "detail": f"user {self._username}" if self._username else None}

    async def root_folders(self) -> list[RootFolder]:
        return []  # Storyteller ingests by explicit path, not a fixed root-folder set

    async def list_books(self) -> list[dict]:
        """All books, normalized to: uuid, title, author, has_ebook, has_audio, readaloud_status."""
        data = await self._get(f"{API}/books", headers=await self._auth())
        books = data if isinstance(data, list) else (data or {}).get("books", [])
        out = []
        for b in books or []:
            ra = b.get("readaloud") or {}
            authors = b.get("authors") or []
            out.append({
                "uuid": b.get("uuid") or b.get("id"),
                "title": b.get("title"),
                "author": (authors[0].get("name") if authors and isinstance(authors[0], dict)
                           else None),
                "has_ebook": b.get("ebook") is not None,
                "has_audio": b.get("audiobook") is not None,
                "readaloud_status": ra.get("status") if isinstance(ra, dict) else None,
            })
        return out

    async def create_book(self, paths: list[str], *, collection: str | None = None) -> dict:
        """Import file(s) already on a Storyteller-visible path into a (new or matched) book."""
        body: dict = {"paths": list(paths)}
        if collection:
            body["collection"] = collection
        return await self._post(f"{API}/books", headers=await self._auth(), json=body) or {}

    async def process(self, book_id: str) -> None:
        """Kick off (or resume) alignment for a book that now has both an ebook and an audiobook."""
        await self._post(f"{API}/books/{book_id}/process", headers=await self._auth())
