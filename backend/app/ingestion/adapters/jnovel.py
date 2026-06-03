"""J-Novel Club adapter — light novels & manga via the official labs v2 API.

j-novel.club is a JavaScript SPA whose reader lives at ``/read/<slug>-volume-N-part-M``;
the generic web crawler can only see one part. This adapter uses j-novel's documented
JSON API (``labs.j-novel.club/app/v2``) — the same one the official apps use — to
enumerate a series' volumes → parts and fetch each part's content.

The API sits behind Cloudflare, which blocks plain HTTP clients (503/418). We therefore
fetch it through the headless browser (``force_render``), which passes Cloudflare's passive
challenge — the same mechanism the app already uses for ``render_js`` sources.

Reference: a series URL (``https://j-novel.club/series/<slug>``), a ``/read/`` part URL
(its series slug is recovered), or a bare series slug.

**Limits (read before enabling):**
  * Series/volume/part *metadata* enumerates without login. Part *content* is members-only
    — j-novel returns 418/401 without a session, and those chapters are marked failed.
    Set ``SHELF_JNOVEL_AUTH`` to your account ``access_token`` (sent as ``Authorization:
    Bearer``) to fetch content you own.
"""
from __future__ import annotations

import json
import os
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from ..base import (
    ChapterRef,
    ComplianceDeclaration,
    PermanentFetchError,
    RawChapter,
    SourceAdapter,
    WorkMeta,
    registry,
)

API = "https://labs.j-novel.club/app/v2"
_MAX_VOLUMES = 200  # politeness backstop for a single backfill pass


def _series_slug(ref: str) -> str:
    """Recover the series slug from a series URL, a /read/ part URL, or a bare slug."""
    ref = (ref or "").strip()
    if "j-novel.club" not in ref and "/" not in ref:
        return ref
    path = urlparse(ref).path
    m = re.search(r"/series/([^/]+)", path)
    if m:
        return m.group(1)
    m = re.search(r"/read/([^/]+)", path)
    if m:  # a reader URL → strip the trailing -volume-N-part-M
        return re.sub(r"-volume-\d+(?:-part-\d+)?$|-part-\d+$", "", m.group(1), flags=re.I)
    return ref.rstrip("/").rsplit("/", 1)[-1]


def _cover_url(obj: dict | None) -> str | None:
    cov = (obj or {}).get("cover") if isinstance(obj, dict) else None
    if isinstance(cov, dict):
        for k in ("coverUrl", "originalUrl", "thumbnailUrl"):
            if cov.get(k):
                return cov[k]
    return None


def _creators(obj: dict) -> str | None:
    """'creators' is a list of {name, role}; join the author-ish names."""
    names = []
    for c in obj.get("creators") or []:
        if isinstance(c, dict) and c.get("name"):
            role = (c.get("role") or "").upper()
            if role in ("", "AUTHOR", "ILLUSTRATOR", "CREATOR", "ORIGINAL_CREATOR"):
                names.append(c["name"])
    return ", ".join(dict.fromkeys(names)) or None


def _auth_headers() -> dict:
    tok = os.environ.get("SHELF_JNOVEL_AUTH", "").strip()
    return {"Authorization": f"Bearer {tok}"} if tok else {}


@registry.register
class JNovelClubAdapter(SourceAdapter):
    key = "jnovel"
    display_name = "J-Novel Club"
    description = (
        "Light novels & manga via the J-Novel Club labs API (fetched through the headless "
        "browser to pass Cloudflare). Metadata enumerates freely; part CONTENT is members-"
        "only — set SHELF_JNOVEL_AUTH to your access token for content you own. Requires "
        "you to attest you are permitted."
    )
    base_url = "https://j-novel.club"
    enabled = True
    compliance = ComplianceDeclaration(
        license_basis="user-attested",
        tos_permitted_default=False,  # paid service — operator must opt in + attest
        # The labs API path (/app/) is robots-disallowed even though it's the documented API
        # the official apps use. The operator opts in by enabling + attesting; the
        # PoliteFetcher's rate-limit/backoff still applies.
        robots_respected=False,
        needs_attestation=True,
        min_request_interval_s=2.0,
        max_daily_requests=600,
    )

    async def _get_json(self, url: str) -> dict | list:
        # Force the headless browser: the labs API is Cloudflare-fronted and rejects plain
        # HTTP. The rendered page's body innerText is the raw JSON.
        resp = await self.fetcher.get_html(
            self.key, url, headers=_auth_headers(), force_render=True
        )
        text = getattr(resp, "body_text", "") or ""
        if not text.strip():  # fall back to extracting text from the rendered HTML
            text = BeautifulSoup(getattr(resp, "text", "") or "", "lxml").get_text("\n", strip=True)
        text = text.strip()
        try:
            return json.loads(text)
        except Exception as exc:
            raise RuntimeError(f"J-Novel API did not return JSON for {url} ({text[:80]!r})") from exc

    async def discover_work(self, ref: str) -> WorkMeta:
        slug = _series_slug(ref)
        s = await self._get_json(f"{API}/series/{slug}?format=json")
        if isinstance(s, dict) and isinstance(s.get("series"), list) and s["series"]:
            s = s["series"][0]
        if not isinstance(s, dict) or not s.get("id"):
            raise RuntimeError(f"J-Novel series not found for {slug!r}")
        media_kind = "comic" if (s.get("type") or "").upper() == "MANGA" else "text"
        return WorkMeta(
            source_work_ref=slug,
            title=s.get("title") or slug,
            author=None,  # authors live on the volumes' creators; filled in below if present
            description=s.get("description") or s.get("shortDescription"),
            cover_url=_cover_url(s),
            language="en",
            status="ongoing",
            media_kind=media_kind,
        )

    async def list_chapters(self, meta: WorkMeta) -> list[ChapterRef]:
        slug = meta.source_work_ref
        vol_data = await self._get_json(f"{API}/series/{slug}/volumes?format=json")
        volumes = vol_data.get("volumes") if isinstance(vol_data, dict) else vol_data
        refs: list[ChapterRef] = []
        idx = 1
        for vol in (volumes or [])[:_MAX_VOLUMES]:
            vid = vol.get("id") or vol.get("legacyId")
            if not vid:
                continue
            try:
                part_data = await self._get_json(f"{API}/volumes/{vid}/parts?format=json")
            except Exception:
                continue  # one volume failing shouldn't abort the whole series
            parts = part_data.get("parts") if isinstance(part_data, dict) else part_data
            for part in parts or []:
                pid = part.get("id") or part.get("legacyId")
                if not pid:
                    continue
                refs.append(
                    ChapterRef(
                        source_chapter_ref=str(pid),
                        index=idx,
                        title=part.get("title") or f"Part {idx}",
                    )
                )
                idx += 1
        return refs

    async def fetch_chapter(self, ref: ChapterRef) -> RawChapter:
        pid = ref.source_chapter_ref
        url = f"{API}/parts/{pid}/data.xhtml"
        resp = await self.fetcher.get_html(self.key, url, headers=_auth_headers(), force_render=True)
        status = getattr(resp, "status_code", 200)
        body = getattr(resp, "text", "") or ""
        # j-novel gates content behind a membership: it returns 401/403/418 (its "BLITZ"
        # banner) when the part isn't accessible to the caller. The banner can sit past the
        # first line of the rendered text, so scan a generous prefix (not just the first 40
        # chars) for its distinctive marker.
        body_text = (getattr(resp, "body_text", "") or "")
        if status in (401, 403, 418) or "BLITZ" in body_text[:1000]:
            # Permanent (not transient): retrying without credentials only thrashes the
            # source budget — the scheduler marks these 'unavailable' instead of retrying.
            raise PermanentFetchError(
                "J-Novel part is members-only — set SHELF_JNOVEL_AUTH to your account "
                "access token to fetch content you own."
            )
        resp.raise_for_status()
        # Resolve relative image/asset URLs so illustrations load in the reader.
        if "<img" in body:
            body = re.sub(
                r'(<img[^>]+src=")(?!https?:|/api/)([^"]+)"',
                lambda m: m.group(1) + urljoin(url, m.group(2)) + '"',
                body,
            )
        return RawChapter(title=ref.title, body=body, fmt="html")
