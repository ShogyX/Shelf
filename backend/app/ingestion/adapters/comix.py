"""comix.to adapter — manga via metadata API + rendered-DOM scraping + page enumeration.

Verified against the live site (2026-06). comix.to is a JS SPA with a protected API: chapter
listing/reading need a session cookie + a per-request validated nonce and the responses are
ENCRYPTED (``{"e": …}``). We do NOT defeat that crypto. Instead we use what the site exposes:

  * **metadata** — open endpoint ``GET api.comix.to/api/v1/manga/<hid>`` (plain JSON);
  * **chapter list** — the manga page renders chapter links in its DOM and paginates with
    ``?page=N``; we render each page (headless browser) and scrape ``/title/<slug>/<id>-chapter-<n>``
    anchors, de-duplicating by chapter number, until a page adds nothing new;
  * **pages** — the reader renders real, sequentially-named image URLs
    (``…/<token>/01.webp``); we read ONE from the rendered reader to get the per-chapter token
    dir, then enumerate ``01..N`` directly off the (open, no-referer) image CDN.

Selectors/paths track comix.to's current markup; ``api_base`` is overridable via Source.config.
Reference: a series URL (``https://comix.to/title/<hid>-<slug>``) or a bare ``<hid>``/``<hid>-<slug>``.
"""
from __future__ import annotations

import asyncio
import json
import re
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from ..base import (
    ChapterRef,
    ComplianceDeclaration,
    RawChapter,
    SourceAdapter,
    WorkMeta,
    registry,
)

_API = "https://api.comix.to/api/v1"
_SITE = "https://comix.to"
_MAX_LIST_PAGES = 80         # chapter-list pages to walk (≈20 chapters each) — politeness backstop
_MAX_PAGES = 400             # per-chapter image-page enumeration cap
_PAGE_IMG = re.compile(r"^(?P<base>.*/)(?P<num>\d{1,4})\.(?P<ext>webp|jpe?g|png|gif)(?:[?#].*)?$", re.I)
_STATUS = {"completed": "complete", "finished": "complete", "cancelled": "complete",
           "ongoing": "ongoing", "on_hiatus": "ongoing", "hiatus": "ongoing"}


def _series_ref(ref: str) -> str:
    ref = (ref or "").strip()
    if "comix.to" in ref or ref.startswith("/") or "://" in ref:
        path = urlparse(ref if "://" in ref else "https://" + ref).path
        m = re.search(r"/title/([^/]+)", path)
        if m:
            return m.group(1)
        return path.rstrip("/").rsplit("/", 1)[-1]
    return ref


def _hid(ref: str) -> str:
    return _series_ref(ref).split("-", 1)[0]


def _deslug_title(slug: str) -> str:
    """A human-ish title from a URL slug remainder (``vagabond`` → ``Vagabond``). Fallback for when
    the metadata API is token-gated and can't hand us the real title; hook_entry then carries the
    catalog row's richer title/cover/synopsis over the top."""
    return re.sub(r"\s+", " ", (slug or "").replace("-", " ")).strip().title()


def _comix_authors(m: dict) -> str | None:
    """Author/artist credit from a comix `/manga/<hid>` payload (was hardcoded None). Defensive:
    the API exposes credits under a few shapes (list of names or of {name:…}); any absent/odd field
    just yields None, so this never breaks a fetch."""
    names: list[str] = []
    for key in ("authors", "artists", "author", "artist"):
        v = m.get(key)
        for a in (v if isinstance(v, list) else [v] if v else []):
            n = a.get("name") if isinstance(a, dict) else a
            if isinstance(n, str) and n.strip():
                names.append(n.strip())
    return ", ".join(dict.fromkeys(names)) or None


@registry.register
class ComixAdapter(SourceAdapter):
    key = "comix"
    display_name = "Comix.to"
    description = (
        "Manga from comix.to. Metadata via its open API; the chapter list + page images are read "
        "from the rendered reader (headless browser) and the open image CDN. Requires the "
        "headless-browser 'render' extra, and that you attest you are permitted."
    )
    base_url = _SITE
    enabled = True
    compliance = ComplianceDeclaration(
        license_basis="user-attested",
        tos_permitted_default=False,
        robots_respected=False,
        needs_attestation=True,
        min_request_interval_s=3.0,
        max_daily_requests=400,
    )

    @property
    def _api(self) -> str:
        return (self.config.get("api_base") or _API).rstrip("/")

    async def _get_json(self, url: str) -> dict:
        resp = await self.fetcher.get_html(self.key, url, force_render=True)
        text = (getattr(resp, "body_text", "") or "").strip()
        if not text:
            text = BeautifulSoup(getattr(resp, "text", "") or "", "lxml").get_text(strip=True)
        try:
            data = json.loads(text)
        except Exception as exc:
            raise RuntimeError(f"comix API did not return JSON for {url} ({text[:80]!r})") from exc
        return data.get("result", data) if isinstance(data, dict) else {}

    async def discover_work(self, ref: str) -> WorkMeta:
        hid = _hid(ref)
        ref_slug = _series_ref(ref)  # "<hid>-<slug>" (or a bare hid) — the /title/ path segment
        # Metadata came from comix's once-open ``/manga/<hid>`` API. It now requires a per-request
        # signed token ({"message":"Missing token."}) that we don't forge, so treat the API as
        # best-effort: on any failure, fall back to a slug-derived title. The chapter list + page
        # images are read from the rendered reader (list_chapters/fetch_chapter), which needs no
        # token, and hook_entry carries the catalog row's cover/synopsis/author — so a hook still
        # fully succeeds without the API.
        try:
            m = await self._get_json(f"{self._api}/manga/{hid}")
        except Exception:  # noqa: BLE001 — API is optional; the reader paths below don't need it
            m = {}
        if isinstance(m, dict) and m.get("hid"):
            poster = m.get("poster") or {}
            cover = (poster.get("large") or poster.get("medium")) if isinstance(poster, dict) else None
            slug = (m.get("url") or f"/title/{hid}").rstrip("/").rsplit("/title/", 1)[-1]
            return WorkMeta(
                source_work_ref=slug,
                title=m.get("title") or _deslug_title(slug.split("-", 1)[-1]) or hid,
                author=_comix_authors(m),
                description=m.get("synopsis") or None,
                cover_url=cover,
                language="en",
                status=_STATUS.get((m.get("status") or "").lower(), "ongoing"),
                media_kind="comic",
            )
        # API unavailable → minimal metadata from the slug; hook_entry enriches from the catalog row.
        title = _deslug_title(ref_slug.split("-", 1)[1]) if "-" in ref_slug else _deslug_title(ref_slug)
        return WorkMeta(source_work_ref=ref_slug, title=title or hid, media_kind="comic",
                        language="en", status="ongoing")

    async def list_chapters(self, meta: WorkMeta) -> list[ChapterRef]:
        slug = meta.source_work_ref
        anchor = re.compile(
            rf"/title/{re.escape(slug)}/(\d+)-chapter-([0-9]+(?:\.[0-9]+)?)", re.I
        )
        best: dict[float, str] = {}  # chapter number -> first path seen (one group per number)
        page_hits: dict[float, int] = {}  # chapter number -> how many pages it appeared on
        pages_scanned = 0
        for page in range(1, _MAX_LIST_PAGES + 1):
            url = f"{_SITE}/title/{slug}?page={page}"
            resp = await self.fetcher.get_html(self.key, url, force_render=True, scroll=2)
            html = getattr(resp, "text", "") or ""
            pages_scanned += 1
            seen_here: set[float] = set()
            added = 0
            for cid, num in anchor.findall(html):
                try:
                    key = float(num)
                except ValueError:
                    continue
                if key not in seen_here:
                    seen_here.add(key)
                    page_hits[key] = page_hits.get(key, 0) + 1
                if key not in best:
                    best[key] = f"/title/{slug}/{cid}-chapter-{num}"
                    added += 1
            if added == 0:  # this page revealed no new chapter number → we've walked them all
                break
        # Drop phantom anchors: the page chrome links a fixed "read latest" chapter on EVERY list
        # page, so a number that recurs across pages is UI, not a list item (a genuinely paginated
        # chapter appears on exactly one page). But recurrence ALONE isn't enough — on most series
        # the "read latest" button targets the real newest chapter, which also legitimately tops
        # page 1's list. So only drop a recurring number that is ALSO an OUTLIER: far from every
        # other listed number. That catches the dangerous case (comix's One Piece (Colored) listed a
        # phantom 'chapter 1181' on every page while the real run topped out at 1076 — gap 105) while
        # never dropping a real latest chapter, which always sits one step above the rest of the run.
        if pages_scanned >= 3 and len(best) > 2:
            nums = sorted(best)
            for key, hits in list(page_hits.items()):
                if hits < 3:
                    continue
                others = [n for n in nums if n != key]
                nearest = min(abs(key - n) for n in others) if others else 0
                if nearest > 10:  # isolated + recurring → page chrome, not a chapter
                    best.pop(key, None)
        ordered = sorted(best.items(), key=lambda kv: kv[0])
        return [
            ChapterRef(source_chapter_ref=path, index=i, title=f"Chapter {num:g}")
            for i, (num, path) in enumerate(ordered, start=1)
        ]

    async def fetch_chapter(self, ref: ChapterRef) -> RawChapter:
        url = ref.source_chapter_ref
        if url.startswith("/"):
            url = urljoin(_SITE, url)
        # The reader only keeps a few <img> in the DOM, but they're sequentially named under a
        # per-chapter token dir. Render to read ONE, derive the dir, then enumerate the rest.
        # Anti-bot/Cloudflare blocks are detected centrally in the fetcher (raises RateLimited).
        resp = await self.fetcher.get_html(self.key, url, force_render=True, scroll=4)
        soup = BeautifulSoup(getattr(resp, "text", "") or "", "lxml")
        base = ext = pad = None
        for im in soup.find_all("img"):
            src = (im.get("src") or im.get("data-src") or im.get("currentSrc") or "").strip()
            m = _PAGE_IMG.match(src)
            if m:
                base, ext, pad = m.group("base"), m.group("ext"), len(m.group("num"))
                break
        if not base:
            raise RuntimeError("comix reader exposed no page image (site markup may have changed)")
        urls = await self._enumerate_pages(base, ext, pad)
        if not urls:
            raise RuntimeError("comix page enumeration found no images")
        figs = "\n".join(f'<figure class="comic-page"><img src="{u}" alt=""/></figure>' for u in urls)
        return RawChapter(title=ref.title, body=f'<div class="comic">{figs}</div>')

    async def _enumerate_pages(self, base: str, ext: str, pad: int) -> list[str]:
        """Walk base+NN.ext off the open image CDN until a page is missing (tries the reader's
        zero-padding, then unpadded/3-digit before concluding the chapter ended)."""
        out: list[str] = []
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as cl:
            for i in range(1, _MAX_PAGES + 1):
                cands = list(dict.fromkeys(
                    [f"{base}{i:0{pad}d}.{ext}", f"{base}{i}.{ext}", f"{base}{i:03d}.{ext}"]
                ))
                hit = None
                for u in cands:
                    try:
                        r = await cl.head(u)
                        if r.status_code == 200:
                            hit = u
                            break
                    except Exception:  # noqa: BLE001 — network blip on one candidate
                        continue
                if not hit:
                    break
                out.append(hit)
                if i % 10 == 0:
                    await asyncio.sleep(0.2)  # be polite to the CDN
        return out
