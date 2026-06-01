"""Shared integration plumbing: a normalized work model + a base HTTP client.

Each integration (Readarr, Kapowarr) maps its native objects into ``ExternalWork`` so
the rest of Shelf (catalog, matching, metadata copy) is provider-agnostic.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

import httpx

log = logging.getLogger("shelf.integrations")


class IntegrationError(Exception):
    """A connection / auth / protocol error talking to an integration."""


@dataclass
class ExternalWork:
    """A book/comic/novel as reported by an integration, in Shelf's normal form."""

    provider: str          # "readarr" | "kapowarr"
    ref: str               # stable external id (foreignBookId / comicvine_id)
    title: str
    author: str | None = None
    year: int | None = None
    overview: str | None = None
    cover_url: str | None = None
    media_kind: str = "text"   # "text" | "comic"
    in_library: bool = False   # already added to the integration's library
    downloaded: bool = False   # files present (will be found by folder ingestion)
    url: str | None = None     # link to the item in the integration / metadata source
    extra: dict = field(default_factory=dict)


@dataclass
class RootFolder:
    id: int | None
    path: str


_TAG_RE = re.compile(r"<[^>]+>")


def strip_html(text: str | None) -> str | None:
    """Comic synopses often arrive as HTML; keep just the readable text."""
    if not text:
        return None
    out = _TAG_RE.sub(" ", text)
    out = re.sub(r"\s+", " ", out).strip()
    return out or None


class BaseClient:
    provider = "base"
    timeout = 15.0

    def __init__(self, base_url: str, api_key: str) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""

    async def _request(
        self, method: str, path: str, *,
        headers: dict | None = None, params: dict | None = None, json: dict | None = None,
    ):
        url = f"{self.base_url}{path}"
        try:
            async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
                resp = await client.request(
                    method, url, headers=headers or {}, params=params or {}, json=json
                )
        except Exception as exc:  # noqa: BLE001 — surface a clean message
            raise IntegrationError(
                f"{self.provider}: cannot reach {self.base_url} ({exc})"
            ) from exc
        if resp.status_code in (401, 403):
            raise IntegrationError(f"{self.provider}: unauthorized — check the API key")
        if resp.status_code >= 400:
            raise IntegrationError(
                f"{self.provider}: HTTP {resp.status_code} from {path}: {resp.text[:200]}"
            )
        if resp.status_code == 204 or not resp.content:
            return {}
        try:
            return resp.json()
        except Exception as exc:  # noqa: BLE001
            raise IntegrationError(f"{self.provider}: non-JSON response from {path}") from exc

    async def _get(self, path: str, *, headers: dict | None = None, params: dict | None = None):
        return await self._request("GET", path, headers=headers, params=params)

    async def _post(
        self, path: str, *,
        headers: dict | None = None, params: dict | None = None, json: dict | None = None,
    ):
        return await self._request("POST", path, headers=headers, params=params, json=json)

    # --- interface (subclasses implement) ---------------------------------
    async def test_connection(self) -> dict:  # pragma: no cover - interface
        raise NotImplementedError

    async def list_library(self) -> list[ExternalWork]:  # pragma: no cover
        raise NotImplementedError

    async def lookup(self, term: str) -> list[ExternalWork]:  # pragma: no cover
        raise NotImplementedError

    async def root_folders(self) -> list[RootFolder]:  # pragma: no cover
        raise NotImplementedError

    async def grab(  # pragma: no cover - interface
        self, extra: dict, *, root_folder: str | None = None,
        quality_profile_id: int | None = None, metadata_profile_id: int | None = None,
    ) -> dict:
        """Add the work to the service's library + trigger a search/download."""
        raise NotImplementedError


def client_for(integration) -> BaseClient:
    """Construct the right client for an Integration row."""
    from .kapowarr import KapowarrClient
    from .readarr import ReadarrClient

    if integration.kind == "readarr":
        return ReadarrClient(integration.base_url, integration.api_key)
    if integration.kind == "kapowarr":
        return KapowarrClient(integration.base_url, integration.api_key)
    raise IntegrationError(f"unknown integration kind: {integration.kind!r}")
