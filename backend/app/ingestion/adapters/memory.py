"""In-memory dummy adapter — used to round-trip the engine end-to-end without network.

Hooking ref `demo` produces a multi-chapter work whose chapters are generated locally,
which lets the slow-crawl scheduler be exercised without touching any real site.
"""
from __future__ import annotations

from ..base import ChapterRef, ComplianceDeclaration, RawChapter, SourceAdapter, WorkMeta, registry

_LOREM = (
    "The lantern guttered as Wei stepped onto the frost-rimed bridge. "
    "Far below, the river carried the last of autumn out to a sea he had never seen. "
    "He tightened his grip on the worn hilt and counted his breaths, as the old master had taught. "
)


@registry.register
class MemoryAdapter(SourceAdapter):
    key = "memory"
    display_name = "In-memory demo"
    description = "Locally-generated demo work for exercising the ingestion engine. No network."
    base_url = None
    enabled = True
    compliance = ComplianceDeclaration(
        license_basis="generated",
        tos_permitted_default=True,
        robots_respected=False,
        needs_attestation=False,
        min_request_interval_s=0.1,
        max_daily_requests=100000,
    )

    async def discover_work(self, ref: str) -> WorkMeta:
        return WorkMeta(
            source_work_ref=ref or "demo",
            title="The Frost-Rimed Bridge (Demo)",
            author="Generated",
            description="A locally generated demo serial used to test the ingestion engine.",
            language="en",
            status="ongoing",
        )

    async def list_chapters(self, meta: WorkMeta) -> list[ChapterRef]:
        return [
            ChapterRef(source_chapter_ref=f"ch-{i}", index=i, title=f"Chapter {i}")
            for i in range(1, 13)
        ]

    async def fetch_chapter(self, ref: ChapterRef) -> RawChapter:
        paras = "".join(f"<p>{_LOREM * 3}</p>" for _ in range(6))
        body = f"<h2>{ref.title}</h2>{paras}"
        return RawChapter(title=ref.title, body=body, fmt="html")
