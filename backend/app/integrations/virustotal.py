"""VirusTotal client — file-hash reputation lookup (API v3).

DB-lookup only: ``GET /api/v3/files/{sha256}`` with an ``x-apikey`` header. We never upload files
(privacy + quota). A 404 means the hash is unknown to VirusTotal (not in its database).
"""
from __future__ import annotations

from .base import BaseClient, IntegrationError

BASE = "https://www.virustotal.com"
# A hash VirusTotal always knows (the empty file) — used as a cheap connectivity/key check.
_EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


class VirusTotalClient(BaseClient):
    provider = "virustotal"

    def __init__(self, api_key: str, *, kind: str | None = None, config: dict | None = None) -> None:
        super().__init__(BASE, api_key, kind=kind or "virustotal", config=config)

    def _headers(self) -> dict:
        return {"x-apikey": self.api_key, "accept": "application/json"}

    async def root_folders(self):  # not a download target — keeps the generic test path happy
        return []

    async def lookup(self, sha256: str) -> dict | None:
        """Return last_analysis_stats ({malicious, suspicious, harmless, undetected, ...}) for a file
        hash, or None when VirusTotal has never seen it (HTTP 404 — 'unknown')."""
        try:
            data = await self._get(f"/api/v3/files/{sha256}", headers=self._headers())
        except IntegrationError as exc:
            if "HTTP 404" in str(exc):
                return None
            raise
        attrs = (data or {}).get("data", {}).get("attributes", {}) if isinstance(data, dict) else {}
        return attrs.get("last_analysis_stats") or {}

    async def test_connection(self) -> dict:
        """Validate the key by looking up the empty-file hash (always present). A bad key → 401 → the
        BaseClient raises 'unauthorized', surfaced as a failed test."""
        stats = await self.lookup(_EMPTY_SHA256)
        detail = None
        if stats is not None:
            detail = (f"engines: {sum(int(v or 0) for v in stats.values())} "
                      f"(malicious {int(stats.get('malicious') or 0)})")
        return {"app": "VirusTotal", "version": "v3", "detail": detail}
