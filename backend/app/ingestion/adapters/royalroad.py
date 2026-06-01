"""Royal Road adapter — SHIPPED DISABLED (Stage 8, optional).

Royal Road's Terms of Service must be verified to permit personal copying before
this adapter may be used. Until that verification is done at build time, the adapter
is left disabled and its compliance declares `tos_permitted_default=False`, so the
engine's compliance gate refuses to run it. Do NOT flip these flags without a real,
documented basis.
"""
from __future__ import annotations

from ..base import ChapterRef, ComplianceDeclaration, RawChapter, SourceAdapter, WorkMeta, registry


@registry.register
class RoyalRoadAdapter(SourceAdapter):
    key = "royalroad"
    display_name = "Royal Road (disabled — unverified ToS)"
    description = (
        "Stubbed and DISABLED. Requires documented verification that Royal Road's ToS "
        "permits personal copying before it can be enabled."
    )
    base_url = "https://www.royalroad.com"
    enabled = False
    compliance = ComplianceDeclaration(
        license_basis="unverified",
        tos_permitted_default=False,
        robots_respected=True,
        needs_attestation=True,
        min_request_interval_s=15.0,
        max_daily_requests=100,
    )

    async def discover_work(self, ref: str) -> WorkMeta:
        raise RuntimeError("royalroad adapter is disabled pending ToS verification")

    async def list_chapters(self, meta: WorkMeta) -> list[ChapterRef]:
        raise RuntimeError("royalroad adapter is disabled pending ToS verification")

    async def fetch_chapter(self, ref: ChapterRef) -> RawChapter:
        raise RuntimeError("royalroad adapter is disabled pending ToS verification")
