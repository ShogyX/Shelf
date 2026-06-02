"""MangaDex adapter — manga via the official MangaDex API.

MangaDex's site is a JS SPA and its image delivery (`/at-home/`) is robots-disallowed,
so the website can't be crawled into readable chapters. This adapter instead uses the
documented JSON API (api.mangadex.org): manga metadata, the chapter feed, and per-chapter
"at-home" image servers. Because the API's `/at-home/` path is robots-disallowed, the
source ships **disabled by default** and is robots-unrespected once an operator enables
it — enable only for content you are permitted to read.

Reference can be a MangaDex title URL (``https://mangadex.org/title/<uuid>/<slug>``) or a
bare manga UUID.
"""
from __future__ import annotations

import re

from ..base import (
    ChapterRef,
    ComplianceDeclaration,
    RawChapter,
    SourceAdapter,
    WorkMeta,
    registry,
)

API = "https://api.mangadex.org"
UPLOADS = "https://uploads.mangadex.org"
_UUID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I
)
_MAX_CHAPTERS = 500  # politeness cap for a single backfill pass


def _pick_title(title_map: dict, alts: list | None, fallback: str) -> str:
    if title_map.get("en"):
        return title_map["en"]
    for alt in alts or []:
        if isinstance(alt, dict) and alt.get("en"):
            return alt["en"]
    return next(iter(title_map.values()), fallback) if title_map else fallback


@registry.register
class MangaDexAdapter(SourceAdapter):
    key = "mangadex"
    display_name = "MangaDex"
    description = "Manga via the MangaDex API (pages served from at-home image servers)."
    base_url = "https://mangadex.org"
    enabled = True
    compliance = ComplianceDeclaration(
        license_basis="user-attested",
        tos_permitted_default=False,  # operator must opt in (robots-disallowed image path)
        robots_respected=False,
        needs_attestation=True,
        # ~0.83 req/s — under the API's global ~5 req/s ceiling. The /at-home endpoint has a
        # tighter (~40/min) sub-limit; the PoliteFetcher's adaptive 429/Retry-After backoff
        # is what actually enforces it, slowing down if MangaDex pushes back.
        min_request_interval_s=1.2,
        max_daily_requests=600,
    )

    @staticmethod
    def _manga_id(ref: str) -> str:
        m = _UUID.search(ref or "")
        return m.group(0) if m else (ref or "").strip().rstrip("/").rsplit("/", 1)[-1]

    async def discover_work(self, ref: str) -> WorkMeta:
        mid = self._manga_id(ref)
        resp = await self.fetcher.get(
            self.key, f"{API}/manga/{mid}?includes[]=cover_art&includes[]=author"
        )
        resp.raise_for_status()
        body = resp.json()
        data = body.get("data")
        if not isinstance(data, dict):
            raise RuntimeError(f"Unexpected MangaDex response for manga {mid}")
        attr = data.get("attributes") or {}
        title = _pick_title(attr.get("title") or {}, attr.get("altTitles"), mid)
        desc = (attr.get("description") or {}).get("en")
        author = None
        cover = None
        for rel in data.get("relationships", []):
            ra = rel.get("attributes") or {}
            if rel.get("type") == "cover_art" and ra.get("fileName"):
                cover = f"{UPLOADS}/covers/{mid}/{ra['fileName']}.512.jpg"
            elif rel.get("type") == "author" and ra.get("name") and not author:
                author = ra["name"]
        status = "complete" if attr.get("status") == "completed" else "ongoing"
        return WorkMeta(
            source_work_ref=mid,
            title=title,
            author=author,
            description=desc,
            cover_url=cover,
            language="en",
            status=status,
        )

    async def list_chapters(self, meta: WorkMeta) -> list[ChapterRef]:
        mid = meta.source_work_ref
        raw: list[tuple[str, str | None]] = []
        offset = 0
        while offset < _MAX_CHAPTERS:
            resp = await self.fetcher.get(
                self.key,
                f"{API}/manga/{mid}/feed?translatedLanguage[]=en"
                f"&order[chapter]=asc&limit=100&offset={offset}"
                f"&contentRating[]=safe&contentRating[]=suggestive&contentRating[]=erotica",
            )
            resp.raise_for_status()
            payload = resp.json()
            if payload.get("result") == "error":
                break
            batch = payload.get("data") or []
            for ch in batch:
                a = ch.get("attributes") or {}
                if a.get("externalUrl") or not (a.get("pages") or 0):
                    continue  # skip off-site / empty chapters (nothing to read)
                if ch.get("id"):
                    raw.append((ch["id"], a.get("chapter")))
            offset += 100
            # A short page is the canonical last-page signal; don't trust `total` (it can
            # be missing/0 while data is still returned, which would truncate the series).
            if len(batch) < 100:
                break
        # Collapse duplicate chapter numbers (multiple scan groups) → first seen.
        refs: list[ChapterRef] = []
        seen: set[str] = set()
        idx = 1
        for cid, num in raw:
            key = num or cid
            if key in seen:
                continue
            seen.add(key)
            refs.append(
                ChapterRef(
                    source_chapter_ref=cid,
                    index=idx,
                    title=f"Chapter {num}" if num else "Oneshot",
                )
            )
            idx += 1
        return refs

    async def fetch_chapter(self, ref: ChapterRef) -> RawChapter:
        cid = ref.source_chapter_ref
        resp = await self.fetcher.get(self.key, f"{API}/at-home/server/{cid}")
        resp.raise_for_status()
        home = resp.json()
        base = home.get("baseUrl")
        chapter = home.get("chapter") or {}
        h = chapter.get("hash")
        if not base or not h:
            raise RuntimeError(f"MangaDex at-home server gave no image host for chapter {cid}")
        files = chapter.get("data") or chapter.get("dataSaver") or []
        seg = "data" if chapter.get("data") else "data-saver"
        pages = "".join(
            f'<figure class="comic-page"><img src="{base}/{seg}/{h}/{fn}" alt="page {i}"/></figure>'
            for i, fn in enumerate(files, start=1)
        )
        body = f'<div class="comic">{pages}</div>' if pages else "<p>(no pages)</p>"
        return RawChapter(title=ref.title, body=body, fmt="html")
