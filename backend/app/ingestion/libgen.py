"""Open-library FALLBACK download pipeline (Library Genesis & friends).

A second acquisition route, used when the usenet pipeline (Prowlarr→SABnzbd) finds no match or isn't
installed. It searches a set of free, no-account open-library mirrors, downloads the best match
directly over HTTP, content-verifies the file (same gate as the usenet path), and imports it into the
library. Providers are a cascade: each candidate is downloaded + verified, and a wrong/dead one is
skipped to the next — across mirrors and across providers.

Adherence is about being a polite client, NOT about anything else: every request goes through a
per-host rate limiter (a minimum interval between requests, a daily request cap, a global concurrency
cap, and Retry-After/429/503 backoff). The Cloudflare-fronted sites are fetched through the shared
headless browser; the rest use plain HTTP.

Providers (config-ordered, only the enabled ones run):
  * libgen     — the Format-2 Library Genesis mirror family (libgen.la/.li/.gl/.bz/.vg), one shared
                 database; search the results table, resolve ads.php→get.php, stream the file.
  * annas      — Anna's Archive search (shares MD5s with libgen) → download the MD5 via libgen.
  * zlibrary   — z-library.sk via the headless browser (optional account for the download link).
  * oceanofpdf — oceanofpdf.com via the headless browser.
  * liber3     — liber3 gateway (best-effort; flaky public gateway).
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import httpx
from .. import telemetry
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import CatalogWork, DownloadJob, Integration, Work
from . import broken, verify
from .extract import authors_compatible, norm_title

if TYPE_CHECKING:  # only for the "matchmeta.WorkMeta" string annotations (imported lazily at runtime)
    from . import matchmeta  # noqa: F401

log = logging.getLogger("shelf.libgen")

KIND = "libgen"                       # Integration.kind + DownloadJob.grab_kind for this pipeline
ROUTE = "libgen"                      # acquire.py route key

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/124.0.0.0 Safari/537.36")
_HTML_HEADERS = {
    "User-Agent": _UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Provider notes (HOW each is searched + its quirks — keep current when touching a provider):
#   libgen     — index.php?req=<title>&columns[]=t&objects[]=f (TITLE-ONLY; author hurts a single-
#                column search). Parsed from the #tablelibgen rows. Download via ads.php→get.php on a
#                mirror. The mirror DOWNLOAD backend is frequently 503 (overloaded) — the real cause
#                of "fails verify": no file arrives, so nothing verifies.
#   annas      — annas-archive.gl/search?q=<title>; cards expose the same MD5s, downloaded through the
#                SAME libgen mirrors → it does NOT help when those mirrors are 503. Anna's own
#                fast_download needs a paid membership key; slow_download is heavily rate-limited.
#   zlibrary   — z-library.sk/s/<title> via the headless browser. Results are JS-rendered <z-bookcard>
#                elements (captured inconsistently) and DOWNLOADS REQUIRE A LOGIN (zlib_user/zlib_pass).
#                Best-effort fallback only.
#   oceanofpdf — oceanofpdf.com/?s=<title> via the headless browser; sits behind a Cloudflare managed
#                challenge the headless browser usually can't pass. Best-effort fallback only.
#   liber3     — no stable public endpoint; registered for the future, currently a no-op.
# Browser-based providers are SLOW (a render each) and unreliable, so search_book only falls back to
# them when the fast providers found nothing importable.
DEFAULT_PROVIDERS = ["libgen", "annas", "zlibrary", "oceanofpdf"]   # liber3 has no endpoint yet
ALL_PROVIDERS = ["libgen", "annas", "zlibrary", "oceanofpdf", "liber3"]
_FALLBACK_PROVIDERS = {"zlibrary", "oceanofpdf", "liber3"}          # browser-based / unreliable
DEFAULT_LIBGEN_HOSTS = ["libgen.la", "libgen.gl", "libgen.bz", "libgen.vg", "libgen.li"]
DEFAULT_MIN_INTERVAL_S = 2.0          # min seconds between requests to the SAME host
DEFAULT_MAX_PER_DAY = 1000            # per-host daily request cap (300 was throttling heavy stocking)
DEFAULT_MAX_CONCURRENT = 3           # global cap on concurrent downloads
DEFAULT_FORMATS = ["epub", "pdf"]    # only formats the importer can ingest
CANDIDATE_CAP = 8                     # most candidates we'll download+verify before giving up
SEARCH_LIMIT = 50                     # results parsed per provider search (title-only search casts a
                                      # wider net, so keep enough rows that the real edition is in-window)
PER_TICK = 3                          # jobs advanced per worker tick (download regulation)
# Transient-failure retry policy: when a job can't be fetched because the endpoint is blocked/timing
# out (NOT because no candidate matched), it stays QUEUED and is retried with growing backoff until
# the endpoint resolves — or until it has failed this many times, after which it's marked failed.
MAX_TRANSIENT_RETRIES = 6
RETRY_BACKOFF_BASE_S = 300            # 5 min, doubling each retry …
RETRY_BACKOFF_MAX_S = 6 * 3600       # … capped at 6 h between attempts


def _retry_backoff_s(retries: int) -> float:
    """Exponential backoff for the Nth transient retry (1-based): 5m, 10m, 20m, … capped at 6h."""
    return float(min(RETRY_BACKOFF_BASE_S * (2 ** max(0, retries - 1)), RETRY_BACKOFF_MAX_S))


def _utcnow() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------- config
def get_integration(db: Session) -> Integration | None:
    return db.scalar(select(Integration).where(
        Integration.kind == KIND, Integration.enabled.is_(True)))


def configured(db: Session) -> bool:
    return get_integration(db) is not None


@dataclass
class Config:
    providers: list[str]
    libgen_hosts: list[str]
    min_interval_s: float
    max_per_day: int
    max_concurrent: int
    formats: list[str]
    download_dir: str | None
    zlib_user: str | None
    zlib_pass: str | None


def load_config(integ: Integration | None) -> Config:
    c = (integ.config if integ else None) or {}
    provs = [p for p in (c.get("providers") or DEFAULT_PROVIDERS) if p in ALL_PROVIDERS]
    return Config(
        providers=provs or DEFAULT_PROVIDERS,
        libgen_hosts=[h.strip() for h in (c.get("libgen_hosts") or DEFAULT_LIBGEN_HOSTS) if h.strip()],
        min_interval_s=float(c.get("min_interval_s", DEFAULT_MIN_INTERVAL_S) or DEFAULT_MIN_INTERVAL_S),
        max_per_day=int(c.get("max_per_day", DEFAULT_MAX_PER_DAY) or DEFAULT_MAX_PER_DAY),
        max_concurrent=max(1, int(c.get("max_concurrent", DEFAULT_MAX_CONCURRENT) or DEFAULT_MAX_CONCURRENT)),
        formats=[f.lower() for f in (c.get("formats") or DEFAULT_FORMATS)],
        download_dir=((c.get("download_dir") or "").strip() or None),
        zlib_user=((c.get("zlib_user") or "").strip() or None),
        zlib_pass=(c.get("zlib_pass") or None),
    )


def _target_dir(db: Session, cfg: Config) -> str | None:
    """Where verified files are placed. The pipeline's own download_dir, else the SABnzbd library
    folder (so it joins the watched library like usenet downloads), else None."""
    if cfg.download_dir:
        return cfg.download_dir
    from .downloads import _library_dir, get_sabnzbd
    sab = get_sabnzbd(db)
    return _library_dir(sab) if sab else None


# --------------------------------------------------------------------- rate limiter
class RateLimitExceeded(Exception):
    """The host's daily request cap is spent — try a different host/provider, or wait for tomorrow."""


@dataclass
class _HostState:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last: float = 0.0                 # monotonic of last request
    day: str = ""                     # UTC date of the counter
    count: int = 0
    blocked_until: float = 0.0        # monotonic — set on 429/503 Retry-After


_HOSTS: dict[str, _HostState] = {}
_concurrency: asyncio.Semaphore | None = None
_conc_limit = DEFAULT_MAX_CONCURRENT


def _semaphore(limit: int) -> asyncio.Semaphore:
    """The global download-concurrency cap, created ONCE. Swapping the semaphore when a different
    config limit was seen (the old behavior) silently broke the cap: in-flight downloads already
    holding the OLD semaphore no longer shared the count with new acquirers, so the global limit
    could be exceeded — defeating the whole point on a shared/abused endpoint. The cap is fixed at
    first use; a later config change is logged and ignored until restart."""
    global _concurrency, _conc_limit
    if _concurrency is None:
        _concurrency, _conc_limit = asyncio.Semaphore(limit), limit
    elif limit != _conc_limit:
        log.info("libgen: concurrency cap is fixed at %d for this process (ignoring new %d); "
                 "restart to change it", _conc_limit, limit)
    return _concurrency


async def _throttle(host: str, cfg: Config) -> None:
    """Block until it's polite to hit `host` again: honour the per-host min interval, the daily cap,
    and any active Retry-After backoff. Raises RateLimitExceeded when the daily cap is spent."""
    st = _HOSTS.setdefault(host, _HostState())
    async with st.lock:
        today = _utcnow().strftime("%Y-%m-%d")
        if st.day != today:
            st.day, st.count = today, 0
        if cfg.max_per_day and st.count >= cfg.max_per_day:
            raise RateLimitExceeded(host)
        now = time.monotonic()
        wait = max(st.blocked_until - now, st.last + cfg.min_interval_s - now, 0.0)
        if wait > 0:
            await asyncio.sleep(wait)
        st.last = time.monotonic()
        st.count += 1


def _note_backoff(host: str, retry_after: float) -> None:
    # Never SHRINK an active cooldown: two concurrent downloads each hitting a 503 must not let the
    # shorter Retry-After clobber a longer one (the lost-backoff race). max() makes the longest
    # cooldown win; the single float store is atomic under CPython's GIL, so this needs no lock.
    st = _HOSTS.setdefault(host, _HostState())
    st.blocked_until = max(st.blocked_until, time.monotonic() + max(1.0, retry_after))


def _is_cf_challenge(resp: httpx.Response, body: bytes) -> bool:
    """A Cloudflare ANTI-BOT challenge (worth a browser retry), as opposed to a plain origin error
    (e.g. an overloaded nginx 503, which the browser can't fix). Delegates to the SHARED detector:
    full-body scan (the old [:4000] slice mislabeled verbose challenges as 'throttled' and retried
    them via plain HTTP forever), and ANY non-empty cf-mitigated value counts (block /
    managed_challenge — the old exact-match on 'challenge' missed those)."""
    from .challenge import is_challenge
    return is_challenge(resp.status_code, resp.headers, body)


def _retry_after_seconds(resp: httpx.Response) -> float:
    ra = resp.headers.get("Retry-After")
    if ra:
        try:
            return float(ra)
        except ValueError:
            pass
    return 30.0


# --------------------------------------------------------------------- fetch (httpx + browser)
def _host_of(url: str) -> str:
    m = re.match(r"https?://([^/]+)", url)
    return (m.group(1) if m else url).lower()


class Fetcher:
    """Rate-limited fetch surface shared by all providers. ``render`` routes through the headless
    browser (for Cloudflare-fronted sites) and reuses its clearance cookies for file downloads."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._client: httpx.AsyncClient | None = None
        self._browser = None
        self._cf_cookies: dict[str, dict[str, str]] = {}  # host → cookie dict captured from browser

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = telemetry.instrument("libgen", timeout=30.0, follow_redirects=True, headers=_HTML_HEADERS)
        return self._client

    async def get_html(self, url: str, *, render: bool = False, params: dict | None = None) -> str | None:
        host = _host_of(url)
        try:
            await _throttle(host, self.cfg)
        except RateLimitExceeded:
            log.info("libgen: daily cap reached for %s", host)
            return None
        if render:
            return await self._render_html(url)
        try:
            r = await (await self._http()).get(url, params=params)
        except httpx.HTTPError as exc:
            log.info("libgen GET %s failed: %s", url, exc)
            return None
        if r.status_code in (429, 503):
            _note_backoff(host, _retry_after_seconds(r))
            log.info("libgen %s throttled (HTTP %s)", host, r.status_code)
            return None
        return r.text if r.status_code == 200 else None

    async def _render_html(self, url: str) -> str | None:
        host = _host_of(url)
        try:
            from .browser import BrowserFetcher
            if self._browser is None:
                self._browser = BrowserFetcher(user_agent=_UA)
            page = await self._browser.render(url)
            # Capture clearance cookies so the file download (httpx) gets past Cloudflare too.
            try:
                ctx = await self._browser._ensure()
                self._cf_cookies[host] = {c["name"]: c["value"] for c in await ctx.cookies()}
            except Exception:  # noqa: BLE001
                pass
            return getattr(page, "text", None)   # RenderedPage stores the HTML on .text, not .html
        except Exception as exc:  # noqa: BLE001 — render is best-effort
            log.info("libgen render %s failed: %s", url, exc)
            return None

    async def download(self, url: str, dest: str, *, render_host: str | None = None,
                       referer: str | None = None) -> str:
        """Stream `url` to `dest`. Returns one of:
          "ok"        — file written;
          "blocked"   — a Cloudflare anti-bot CHALLENGE (worth retrying through the headless browser);
          "throttled" — a TRANSIENT endpoint problem (rate-limit, 429/503, connection/timeout error) —
                        the link may well work once the endpoint recovers, so the JOB should stay
                        queued and be retried, NOT advanced/abandoned;
          "fail"      — a TERMINAL problem for THIS link (origin 4xx, wrong content, not a file) — try
                        the next candidate.
        Reuses browser clearance cookies when `render_host` is set."""
        host = _host_of(url)
        try:
            await _throttle(host, self.cfg)
        except RateLimitExceeded:
            return "throttled"          # per-host daily cap — transient, retry after the day rolls over
        headers = dict(_HTML_HEADERS)
        if referer:
            headers["Referer"] = referer
        cookies = self._cf_cookies.get(render_host) if render_host else None
        try:
            async with (await self._http()).stream("GET", url, headers=headers, cookies=cookies) as r:
                if r.status_code == 200:
                    ctype = (r.headers.get("content-type") or "").lower()
                    if "text/html" in ctype:    # a challenge/interstitial served with 200, not a file
                        return "blocked" if _is_cf_challenge(r, await r.aread()) else "fail"
                    tmp = dest + ".part"
                    try:
                        with open(tmp, "wb") as fh:
                            async for chunk in r.aiter_bytes(65536):
                                fh.write(chunk)
                        os.replace(tmp, dest)
                    finally:
                        # A mid-stream error (dropped connection / disk full) leaves the partial
                        # .part behind; remove it so retries with the same dest don't accumulate.
                        if os.path.exists(tmp):
                            try:
                                os.remove(tmp)
                            except OSError:
                                pass
                    return "ok" if os.path.getsize(dest) > 1024 else "fail"
                if r.status_code in (429, 503):
                    _note_backoff(host, _retry_after_seconds(r))
                    body = await r.aread()
                    return "blocked" if _is_cf_challenge(r, body) else "throttled"  # overloaded → retry
                body = await r.aread()
                if _is_cf_challenge(r, body):
                    return "blocked"
                log.info("libgen download %s → HTTP %s (origin)", url, r.status_code)
                return "fail"
        except httpx.TimeoutException as exc:
            log.info("libgen download %s timed out: %s", url, exc)
            return "throttled"          # a timeout is transient — the endpoint may recover
        except httpx.HTTPError as exc:
            log.info("libgen download %s failed: %s", url, exc)
            return "throttled"          # connection/transport error — transient, worth a retry
        except OSError as exc:
            log.info("libgen download write failed: %s", exc)
            return "fail"

    async def download_via_browser(self, url: str, dest: str, *, referer: str | None = None) -> bool:
        """Download a file through the headless browser — for hosts (e.g. LibGen get.php) whose file
        endpoint is behind a Cloudflare challenge that plain HTTP can't pass. The browser solves the
        challenge and the file arrives as a download event."""
        host = _host_of(url)
        try:
            await _throttle(host, self.cfg)
        except RateLimitExceeded:
            return False
        try:
            from .browser import BrowserFetcher
            if self._browser is None:
                self._browser = BrowserFetcher(user_agent=_UA)
            ctx = await self._browser._ensure()
            page = await ctx.new_page()
            try:
                if referer:
                    await page.set_extra_http_headers({"Referer": referer})
                async with page.expect_download(timeout=120_000) as dl_info:
                    try:
                        await page.goto(url, timeout=60_000)
                    except Exception:  # noqa: BLE001 — a navigation that becomes a download "errors"
                        pass
                download = await dl_info.value
                await download.save_as(dest)
            finally:
                await page.close()
            return os.path.isfile(dest) and os.path.getsize(dest) > 1024
        except Exception as exc:  # noqa: BLE001 — best-effort
            log.info("libgen browser download %s failed: %s", url, exc)
            return False

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        if self._browser is not None:
            try:
                await self._browser.aclose()
            except Exception:  # noqa: BLE001
                pass
            self._browser = None


# --------------------------------------------------------------------- hits + matching
@dataclass
class Hit:
    provider: str
    title: str
    author: str | None
    ext: str | None
    size: int | None          # bytes (best-effort)
    year: int | None
    language: str | None
    md5: str | None
    host: str | None          # the mirror host this hit came from (for download)
    page_url: str | None      # provider page to resolve the download from
    direct_url: str | None    # a direct file URL when known
    content_type: str | None = None  # provider type label (libgen "Book"/"Comic"/"Article" badge)

    def key(self) -> str:
        return self.md5 or f"{self.provider}:{self.host}:{self.title}:{self.ext}"


_SIZE_RE = re.compile(r"([\d.]+)\s*(kb|mb|gb|b)\b", re.I)


def _parse_size(text: str | None) -> int | None:
    if not text:
        return None
    m = _SIZE_RE.search(text)
    if not m:
        return None
    n = float(m.group(1))
    mult = {"b": 1, "kb": 1_000, "mb": 1_000_000, "gb": 1_000_000_000}[m.group(2).lower()]
    return int(n * mult)


def _good_format(ext: str | None, cfg: Config) -> bool:
    if not ext:
        return False
    e = ext.lower()
    if e in cfg.formats:
        return True
    # Accept Kindle formats too when a converter is available — they're turned into EPUB on download.
    from . import convert
    return convert.available() and f".{e}" in convert.CONVERTIBLE_EXTS


def _score_hit(meta: "matchmeta.WorkMeta", h: Hit) -> float:
    """Precision-aware title match gated by author compatibility AND content type. The title is scored
    against EVERY known title for the work (display + romaji/english/native/synonyms) and the best
    taken — so a manga hit titled "Shingeki no Kyojin" matches a work catalogued as "Attack on Titan".
    Uses verify's segment-aware scorer (NOT bare recall, which scored 1.0 for any hit merely
    CONTAINING the words — a journal article, a study guide, an omnibus). Finally a type mismatch
    (an article/comic when we want a prose book, or vice-versa) is penalised, never hard-dropped, so
    junk sinks below the real book but a mislabelled-but-correct hit can still win when nothing beats
    it. When the work or hit type is unknown this degrades to pure title/author matching."""
    from . import matchmeta as mm
    from . import verify
    ts = max((verify._title_score(t, h.title or "") for t in meta.titles), default=0.0)
    if meta.author and h.author and not authors_compatible(meta.author, h.author):
        ts *= 0.4
    ts *= mm.type_compat(meta.bucket, mm.bucket_of(h.content_type))
    return ts


def _edition_quality(meta: "matchmeta.WorkMeta", h: Hit) -> float:
    """A SECONDARY ranking signal (NOT part of the title gate) so that among equally-matching hits
    the importable, correct-language, non-garbage edition is tried first — fewer download+verify
    cycles and fewer wrong-language failures. Returns a small score; title match always dominates."""
    q = 0.0
    e = (h.ext or "").lower()
    q += {"epub": 0.30, "pdf": 0.15}.get(e, 0.0)        # epub imports cleanest; pdf ok; others lower
    if meta.language and h.language:
        lang = (h.language or "").lower()
        want = meta.language.lower()
        if want[:2] == lang[:2] or want in lang or lang in want:
            q += 0.20
        else:
            q -= 0.20                                   # wrong language → push down (don't drop)
    if h.size:                                          # size sanity band per format
        if h.size < 30_000:
            q -= 0.30                                   # a few-KB stub, not a real book
        elif e == "pdf" and h.size > 300_000_000:
            q -= 0.15                                   # a 300MB+ scan PDF — huge + often image-only
    if h.author:
        q += 0.05                                       # an edition with author metadata is richer
    return q


def candidates_for(meta: "matchmeta.WorkMeta", hits: list[Hit], cfg: Config) -> list[Hit]:
    """Rank + cap the importable-format hits for a work (best title/author/type match first, then the
    cleaner edition)."""
    scored = [(h, _score_hit(meta, h)) for h in hits if _good_format(h.ext, cfg)]
    scored = [(h, s) for h, s in scored if s >= 0.5]
    # Primary: title/author/type match. Secondary: edition quality (format/language/size) — so two
    # hits of the same book are tried best-edition-first without letting format override a wrong title.
    scored.sort(key=lambda hs: (round(hs[1], 3), _edition_quality(meta, hs[0])), reverse=True)
    out, seen = [], set()
    for h, _ in scored:
        k = h.key()
        if k in seen:
            continue
        seen.add(k)
        out.append(h)
        if len(out) >= CANDIDATE_CAP:
            break
    return out


# --------------------------------------------------------------------- provider: LibGen family
def _libgen_query(cw: CatalogWork) -> str:
    """LibGen searches a single column, so the query is the TITLE ONLY. Appending the author (as we
    used to) requires the author's name to also appear in the matched field — which a plain novel's
    title does NOT — so it filtered out the real book and left only study-guides/omnibuses that
    literally carry the author in their title. The author is applied later, in _score_hit's ranking."""
    return (cw.title or "").strip()


def _libgen_type_badge(td) -> str | None:
    """LibGen tags each row's first cell with a type badge — <span class="badge badge-primary"> whose
    inner link carries title="Book" / "Comic" / "Magazine" / "Article". That's the signal that tells a
    prose novel apart from a comic or a journal article, so we capture it for type-aware ranking."""
    badge = td.find("span", class_=re.compile(r"badge-primary"))
    if badge is not None:
        a = badge.find(attrs={"title": True})
        if a is not None and a.get("title"):
            return a["title"].strip()
        txt = badge.get_text(strip=True)
        if txt:
            return txt
    return None


def _libgen_title_cell(td) -> str:
    """The clean title from a LibGen result's first cell. That cell mixes a series label (<b>), the
    title (an ``edition.php`` link), an ISBN (a green-font link) and badges (<nobr>) — taking the
    whole cell's text polluted the title with ISBNs/badges and wrecked matching. Prefer the edition
    link whose text actually has letters (the title), skipping the ISBN-only link."""
    for a in td.find_all("a", href=re.compile("edition.php")):
        t = " ".join(a.get_text(" ", strip=True).split())
        if t and re.search(r"[A-Za-z]", t):   # the title link — not the ISBN-only one
            return t
    return " ".join(td.get_text(" ", strip=True).split())


def _parse_libgen_table(html: str, host: str) -> list[Hit]:
    """Parse one LibGen search-result page into Hits (clean title + type badge + format)."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", id="tablelibgen") or soup.find("table", class_=re.compile("table"))
    if table is None:
        return []
    hits: list[Hit] = []
    for tr in table.find_all("tr")[1:]:
        tds = tr.find_all("td")
        if len(tds) < 9:
            continue
        md5 = None
        for a in tds[8].find_all("a", href=True):
            m = re.search(r"md5=([a-fA-F0-9]{32})", a["href"])
            if m:
                md5 = m.group(1).lower()
                break
        if not md5:
            continue
        yr = re.search(r"(\d{4})", tds[3].get_text(" ", strip=True))
        hits.append(Hit(
            provider="libgen", title=_libgen_title_cell(tds[0]),
            author=(tds[1].get_text(", ", strip=True) or None),
            ext=(tds[7].get_text(strip=True) or "").lower() or None,
            size=_parse_size(tds[6].get_text(" ", strip=True)),
            year=int(yr.group(1)) if yr else None,
            language=(tds[4].get_text(strip=True) or None),
            md5=md5, host=host, page_url=f"https://{host}/ads.php?md5={md5}", direct_url=None,
            content_type=_libgen_type_badge(tds[0]),
        ))
    return hits


async def _libgen_search(fetcher: Fetcher, cfg: Config, cw: CatalogWork,
                         titles: list[str] | None = None) -> list[Hit]:
    """Search the LibGen mirror family (first host that answers; they share one database). Searches
    EVERY known title for the work (display + alternates) and merges the results, deduped by md5."""
    queries = [t for t in (titles or [_libgen_query(cw)]) if t.strip()] or [_libgen_query(cw)]
    for host in cfg.libgen_hosts:
        merged: dict[str, Hit] = {}
        answered = False
        for q in queries:
            html = await fetcher.get_html(
                f"https://{host}/index.php",
                params={"req": q, "columns[]": "t", "objects[]": "f", "res": str(SEARCH_LIMIT)},
            )
            if not html:
                continue
            answered = True
            for h in _parse_libgen_table(html, host):
                merged.setdefault(h.md5, h)   # first title to surface an md5 wins
        if merged:
            return list(merged.values())
        if answered:
            return []          # host responded but nothing matched any title → don't retry other hosts
    return []


async def _libgen_get_url(fetcher: Fetcher, host: str, md5: str) -> tuple[str, str] | None:
    """Resolve a LibGen mirror's ads.php page to the real ``get.php?md5=&key=`` file URL + referer.
    The key is per-request, so this must run right before the download."""
    ads = f"https://{host}/ads.php?md5={md5}"
    html = await fetcher.get_html(ads)
    if not html:
        return None
    m = re.search(r'href=["\'](/?get\.php\?md5=[a-fA-F0-9]{32}[^"\']*key=[^"\']+)["\']', html, re.I)
    if not m:
        return None
    href = m.group(1).lstrip("/")
    return f"https://{host}/{href}", ads


# --------------------------------------------------------------------- provider: Anna's Archive
async def _annas_search(fetcher: Fetcher, cfg: Config, cw: CatalogWork,
                        titles: list[str] | None = None) -> list[Hit]:
    """Anna's Archive search → MD5s (downloaded via the LibGen mirrors, which share the MD5 space).
    Anna's searches all fields, so each known title is queried and the results merged."""
    from bs4 import BeautifulSoup
    queries = [t for t in (titles or [_libgen_query(cw)]) if t.strip()] or [_libgen_query(cw)]
    hits: list[Hit] = []
    seen: set[str] = set()
    for q in queries:
        html = await fetcher.get_html("https://annas-archive.gl/search", params={"q": q})
        if not html:
            continue
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.find_all("a", href=re.compile(r"/md5/[a-fA-F0-9]{32}")):
            m = re.search(r"/md5/([a-fA-F0-9]{32})", a["href"])
            if not m or m.group(1) in seen:
                continue
            md5 = m.group(1).lower()
            seen.add(md5)
            text = " ".join(a.get_text(" ", strip=True).split())
            # Anna's puts "ext, lang, size · Title · Author" style metadata in the card text, and a
            # leading type word ("Book (fiction)", "Comic book", "Journal article") we keep for typing.
            em = re.search(r"\b(epub|pdf|mobi|azw3|cbz|cbr|fb2|djvu)\b", text, re.I)
            tm = re.search(r"\b(book|comic|magazine|journal\s+article|article|manga)\b", text, re.I)
            hits.append(Hit(
                provider="annas", title=text[:300], author=None,
                ext=em.group(1).lower() if em else None,
                size=_parse_size(text), year=None, language=None,
                md5=md5, host=None, page_url=f"https://annas-archive.gl/md5/{md5}", direct_url=None,
                content_type=tm.group(1) if tm else None,
            ))
            if len(seen) >= SEARCH_LIMIT:
                break
    return hits


# --------------------------------------------------------------- providers: render-based (best-effort)
async def _render_search(fetcher: Fetcher, provider: str, url: str, params: dict | None = None) -> str | None:
    full = url
    if params:
        from urllib.parse import urlencode
        full = f"{url}?{urlencode(params)}"
    return await fetcher.get_html(full, render=True)


async def _zlibrary_search(fetcher: Fetcher, cfg: Config, cw: CatalogWork,
                           titles: list[str] | None = None) -> list[Hit]:
    """z-library.sk via the headless browser. Download needs an account (optional creds); without
    one the candidates are still surfaced but the download step will fall through to other providers.
    Browser renders are expensive, so this searches only the primary title."""
    from bs4 import BeautifulSoup
    from urllib.parse import quote
    query = (titles[0] if titles else _libgen_query(cw))
    html = await fetcher.get_html(f"https://z-library.sk/s/{quote(query)}", render=True)
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    hits: list[Hit] = []
    for card in soup.select("[id^=book], .book-item, z-bookcard"):
        href = card.get("href") or (card.find("a") and card.find("a").get("href"))
        title = card.get("title") or " ".join(card.get_text(" ", strip=True).split())[:200]
        ext = (card.get("extension") or "").lower() or None
        if not href:
            continue
        if href.startswith("/"):
            href = "https://z-library.sk" + href
        hits.append(Hit(provider="zlibrary", title=title, author=card.get("author"), ext=ext,
                        size=None, year=None, language=card.get("language"), md5=None,
                        host="z-library.sk", page_url=href, direct_url=None))
        if len(hits) >= SEARCH_LIMIT:
            break
    return hits


async def _oceanofpdf_search(fetcher: Fetcher, cfg: Config, cw: CatalogWork,
                             titles: list[str] | None = None) -> list[Hit]:
    """oceanofpdf.com via the headless browser (Cloudflare). Best-effort; primary title only."""
    from bs4 import BeautifulSoup
    query = (titles[0] if titles else _libgen_query(cw))
    html = await _render_search(fetcher, "oceanofpdf", "https://oceanofpdf.com/", {"s": query})
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    hits: list[Hit] = []
    for a in soup.select("article a[href*='/authors/'], h2 a[href]"):
        href = a.get("href")
        title = " ".join(a.get_text(" ", strip=True).split())
        if not href or not title:
            continue
        ext = "epub" if "epub" in href.lower() else ("pdf" if "pdf" in href.lower() else None)
        hits.append(Hit(provider="oceanofpdf", title=title, author=None, ext=ext, size=None,
                        year=None, language=None, md5=None, host="oceanofpdf.com",
                        page_url=href, direct_url=None))
        if len(hits) >= SEARCH_LIMIT:
            break
    return hits


async def _liber3_search(fetcher: Fetcher, cfg: Config, cw: CatalogWork,
                         titles: list[str] | None = None) -> list[Hit]:
    """liber3 gateway — best-effort; the public eth.limo gateway is frequently unreachable."""
    return []  # no stable public search endpoint; kept as a registered provider for future use


_PROVIDERS = {
    "libgen": _libgen_search,
    "annas": _annas_search,
    "zlibrary": _zlibrary_search,
    "oceanofpdf": _oceanofpdf_search,
    "liber3": _liber3_search,
}


# --------------------------------------------------------------------- search + download
async def _run_providers(provs: list[str], fetcher: Fetcher, cfg: Config, cw: CatalogWork,
                         titles: list[str]) -> list[Hit]:
    out: list[Hit] = []
    for prov in provs:
        fn = _PROVIDERS.get(prov)
        if fn is None:
            continue
        try:
            hits = await fn(fetcher, cfg, cw, titles)
            log.info("libgen search %s %r (%d title variants) → %d hits",
                     prov, cw.title, len(titles), len(hits))
            out.extend(hits)
        except Exception:  # noqa: BLE001 — one provider must not abort the search
            log.exception("libgen provider %s search failed", prov)
    return out


async def search_book(db: Session, cw: CatalogWork, cfg: Config, fetcher: Fetcher) -> list[Hit]:
    """Run the enabled providers and return ranked, importable-format candidates. The FAST providers
    (libgen/annas) run first; the slow, browser-based fallback providers (zlibrary/oceanofpdf) are only
    tried when the fast ones yielded nothing importable — so the common case stays fast while still
    casting a wider net when the primary mirrors come up empty."""
    from . import matchmeta
    meta = await matchmeta.get_work_meta(db, cw)
    titles = matchmeta.title_variants(meta)
    fast = [p for p in cfg.providers if p not in _FALLBACK_PROVIDERS]
    slow = [p for p in cfg.providers if p in _FALLBACK_PROVIDERS]
    hits = await _run_providers(fast, fetcher, cfg, cw, titles)
    cands = candidates_for(meta, hits, cfg)
    if not cands and slow:
        log.info("libgen: primary providers found nothing for %r → trying fallback %s", cw.title, slow)
        hits += await _run_providers(slow, fetcher, cfg, cw, titles)
        cands = candidates_for(meta, hits, cfg)
    return cands


async def _resolve_download(fetcher: Fetcher, hit: Hit, cfg: Config, dest: str) -> str:
    """Download one candidate to `dest`. LibGen + Anna's resolve via the LibGen ads→get flow (shared
    MD5s); render providers download from their page's direct link with browser cookies. Returns
    "ok" | "throttled" | "fail": "throttled" if EVERY attempt hit a transient block/timeout and none
    succeeded (so the job is retried, not abandoned); "fail" only when an attempt terminally failed
    with no transient blocks (a genuinely dead/wrong link → try the next candidate)."""
    render_host = hit.host if hit.provider in ("zlibrary", "oceanofpdf") else None
    saw_throttle = False

    def _track(st: str) -> str:
        nonlocal saw_throttle
        if st == "throttled":
            saw_throttle = True
        return st

    if hit.direct_url:
        return await _fetch_with_fallback(fetcher, hit.direct_url, dest, referer=hit.page_url,
                                          render_host=render_host)
    # LibGen + Anna's: resolve the MD5 through a LibGen mirror — try every mirror before giving up.
    if hit.md5:
        hosts = [hit.host] if hit.host else []
        hosts += [h for h in cfg.libgen_hosts if h not in hosts]
        for host in hosts:
            got = await _libgen_get_url(fetcher, host, hit.md5)
            if got is None:
                saw_throttle = True       # mirror's ads/get page didn't resolve — treat as transient
                continue
            url, referer = got
            st = _track(await _fetch_with_fallback(fetcher, url, dest, referer=referer))
            if st == "ok":
                return "ok"
        return "throttled" if saw_throttle else "fail"
    # Render providers (z-library / OceanOfPDF): render the book page, pull a download link, fetch it.
    if render_host and hit.page_url:
        url = await _extract_download_link(fetcher, hit)
        if url:
            return await _fetch_with_fallback(fetcher, url, dest, referer=hit.page_url,
                                              render_host=render_host)
        return "throttled"                # couldn't even render the page → transient (browser/CF)
    return "fail"


async def _fetch_with_fallback(fetcher: Fetcher, url: str, dest: str, *, referer: str | None = None,
                               render_host: str | None = None) -> str:
    """Plain HTTP first; only if the host answers with a Cloudflare CHALLENGE (not a plain origin
    error like an overloaded 503) do we spend a headless-browser attempt to solve it. Returns the
    same vocabulary as Fetcher.download: "ok" | "throttled" | "fail" (a CF challenge the browser
    can't solve is reported as "throttled" — the block may lift, so the job should be retried)."""
    status = await fetcher.download(url, dest, referer=referer, render_host=render_host)
    if status == "ok":
        return "ok"
    if status == "blocked":
        if await fetcher.download_via_browser(url, dest, referer=referer):
            return "ok"
        return "throttled"          # still challenged — transient block, retry later
    return status                   # "throttled" | "fail"


async def _extract_download_link(fetcher: Fetcher, hit: Hit) -> str | None:
    """Render a book page and return the first plausible file/download link (.epub/.pdf, /dl/, or a
    link whose text says 'download'). Absolute-ized against the page URL."""
    from urllib.parse import urljoin
    from bs4 import BeautifulSoup
    html = await fetcher.get_html(hit.page_url, render=True)
    if not html:
        return None
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a", href=True):
        href = a["href"]
        text = a.get_text(" ", strip=True).lower()
        if re.search(r"\.(epub|pdf)(\?|$)", href, re.I) or "/dl/" in href.lower() or "download" in text:
            return urljoin(hit.page_url, href)
    return None


# --------------------------------------------------------------------- import (reuses downloads.py)
def _ext_for(hit: Hit) -> str:
    return (hit.ext or "epub").lower().lstrip(".")


def _is_importable_file(path: str) -> bool:
    """True only if the file's ACTUAL bytes are an importable book container: EPUB/CBZ (zip → "PK")
    or PDF ("%PDF"). Reliably rejects mobi/azw3/djvu/lit etc. — whose PDB-style headers are ASCII and
    would fool a text heuristic — regardless of the labelled filename."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(4)
    except OSError:
        return False
    return head[:2] == b"PK" or head[:4] == b"%PDF"


def _import_file(db: Session, path: str, cw: CatalogWork, job: DownloadJob,
                 target_dir: str | None) -> str:
    """Verify a downloaded file and, on success, promote + import it into the library and link it to
    the catalog book + requester. Returns 'imported' | 'retry' | 'failed' and sets job.status."""
    from . import downloads as dl
    from ..library import add_to_library
    from .local_folder import sync_folder

    if not _is_importable_file(path):
        # The labelled format can be wrong (a mirror serves a .mobi for an "epub" entry); a .mobi/
        # .azw3 verifies fine but can't be imported. Reject by ACTUAL content → try the next candidate.
        job.status, job.error = "retry", "downloaded file is not an importable format (need epub/pdf)"
        db.commit()
        return "retry"
    want_title = cw.title or job.title
    want_author = cw.author
    from . import language as lang
    want_lang = lang.canonicalize(cw.language) if cw.language else None
    vr = verify.verify_file(path, want_title, want_author, want_language=want_lang)
    if not vr.ok or not vr.path:
        job.status, job.error = "retry", f"content mismatch ({vr.reason}; conf {vr.confidence:.2f})"
        db.commit()
        return "retry"

    promoted = dl._promote(vr.path, target_dir, want_title)
    if not promoted:
        job.status, job.error = "failed", "verified but could not place the file"
        db.commit()
        return "failed"
    import_root = target_dir or os.path.dirname(promoted)
    folder = dl.ensure_watched_folder(db, import_root)
    if folder is not None:
        try:
            sync_folder(db, folder)
        except Exception:  # noqa: BLE001
            log.exception("libgen import: folder sync failed")
    src = dl._local_source(db)
    work = db.scalar(select(Work).where(Work.source_id == src.id, Work.local_path == promoted))
    if work is None:
        base = os.path.basename(promoted)
        same = db.scalars(select(Work).where(
            Work.source_id == src.id,
            Work.local_path.like(os.path.dirname(promoted).rstrip("/") + "/%"))).all()
        work = next((w for w in same if os.path.basename(w.local_path or "") == base), None)
    if work is None:
        # Promoted but the importer couldn't make a Work (unsupported/odd file) → don't leave an
        # orphan in the library; remove it and report failure so the cascade marks it broken.
        try:
            if os.path.isfile(promoted):
                os.remove(promoted)
                d = os.path.dirname(promoted)
                if os.path.isdir(d) and not os.listdir(d):
                    os.rmdir(d)
        except OSError:
            pass
        job.status, job.error = "failed", f"verified but import produced no Work for {promoted!r}"
        db.commit()
        return "failed"

    job.work_id, job.verified, job.status = work.id, True, "imported"
    job.completed_at = _utcnow()
    if cw.hooked_work_id is None:
        cw.hooked_work_id = work.id
    dl._apply_series(work, cw)
    if job.user_id:
        try:
            add_to_library(db, job.user_id, work.id, shelf_id=job.target_shelf_id)
        except Exception:  # noqa: BLE001
            db.rollback()
            log.exception("libgen add_to_library failed for job %s", job.id)
            job.work_id, job.verified, job.status = work.id, True, "imported"
    db.commit()
    log.info("libgen imported (verified %.2f) %r → work %s", vr.confidence, job.title, work.id)
    return "imported"


# --------------------------------------------------------------------- grab + worker
async def grab(db: Session, cw: CatalogWork, *, user_id: int | None = None,
               shelf_id: int | None = None, context: dict | None = None) -> DownloadJob | None:
    """Search the open libraries for `cw` and create a libgen DownloadJob with the ranked candidate
    cascade. The worker (libgen_tick) downloads + verifies them. Returns None when nothing matched."""
    integ = get_integration(db)
    if integ is None:
        return None
    cfg = load_config(integ)
    fetcher = Fetcher(cfg)
    try:
        hits = await search_book(db, cw, cfg, fetcher)
    finally:
        await fetcher.aclose()
    if not hits:
        return None
    cands = [{
        "provider": h.provider, "title": h.title, "author": h.author, "ext": h.ext,
        "size": h.size, "md5": h.md5, "host": h.host, "page_url": h.page_url,
        "direct_url": h.direct_url, "key": h.key(),
    } for h in hits]
    job = DownloadJob(
        catalog_work_id=cw.id, user_id=user_id, target_shelf_id=shelf_id, title=cw.title,
        status="queued", grab_kind=KIND, candidates=cands, attempt=0,
        release_title=f"{hits[0].provider}: {hits[0].title[:120]}",
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    log.info("libgen grab queued: %r (%d candidates)", cw.title, len(cands))
    return job


def _cands_from_hits(hits: list[Hit]) -> list[dict]:
    return [{
        "provider": h.provider, "title": h.title, "author": h.author, "ext": h.ext,
        "size": h.size, "md5": h.md5, "host": h.host, "page_url": h.page_url,
        "direct_url": h.direct_url, "key": h.key(), "content_type": h.content_type,
    } for h in hits]


async def fetch_for_stock(db: Session, cw: CatalogWork, stock_dir: str) -> DownloadJob | None:
    """Open-library FALLBACK for the stocking pipeline: search, then download + content-verify + import
    the best match INTO THE STOCK DIRECTORY (synchronously, within the stock worker tick). Returns the
    DownloadJob (status 'imported' on success, else 'failed'), or None when no candidate was found at
    all. Used to recover stock items the usenet pipeline couldn't get."""
    integ = get_integration(db)
    if integ is None or not stock_dir:
        return None
    cfg = load_config(integ)
    fetcher = Fetcher(cfg)
    try:
        hits = await search_book(db, cw, cfg, fetcher)
        if not hits:
            return None
        job = DownloadJob(
            catalog_work_id=cw.id, user_id=None, title=cw.title, status="queued",
            grab_kind=KIND, candidates=_cands_from_hits(hits), attempt=0,
            release_title=f"{hits[0].provider}: {hits[0].title[:120]}",
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        await _advance_job(db, job, cfg, fetcher, stock_dir, requeue_on_transient=False)
        db.refresh(job)
        return job
    finally:
        await fetcher.aclose()


def _hit_from_cand(c: dict) -> Hit:
    return Hit(provider=c.get("provider", "libgen"), title=c.get("title") or "",
               author=c.get("author"), ext=c.get("ext"), size=c.get("size"), year=None,
               language=None, md5=c.get("md5"), host=c.get("host"),
               page_url=c.get("page_url"), direct_url=c.get("direct_url"),
               content_type=c.get("content_type"))


async def _advance_job(db: Session, job: DownloadJob, cfg: Config, fetcher: Fetcher,
                       target_dir: str | None, *, requeue_on_transient: bool = True) -> None:
    """Download + verify the job's current candidate; on failure advance to the next; import on
    success. Caps each job at CANDIDATE_CAP attempts.

    On a transient block/timeout (endpoint blocked, not a dead link): when `requeue_on_transient`
    (the worker-driven user-grab path), the job is left QUEUED with backoff and retried by the worker.
    The stock path drives this synchronously and owns its own cooldown-retry, so it passes
    `requeue_on_transient=False` — a transient there just ends this attempt as failed and the stock
    layer recycles the item (a stock job must never be left queued, or the worker would import it into
    the library instead of the stock dir)."""
    cw = db.get(CatalogWork, job.catalog_work_id) if job.catalog_work_id else None
    if cw is None:
        job.status, job.error = "failed", "catalog entry no longer exists"
        db.commit()
        return
    cands = job.candidates or []
    sem = _semaphore(cfg.max_concurrent)
    bad = broken.broken_keys(db)
    while job.attempt < len(cands) and job.attempt < CANDIDATE_CAP:
        cand = cands[job.attempt]
        if cand.get("key") in bad:    # a previously-discarded bad candidate → skip (fresh re-search)
            job.attempt += 1
            db.commit()
            continue
        hit = _hit_from_cand(cand)
        staging = os.path.join(
            (target_dir or tempfile.gettempdir()), ".openlib-staging")
        os.makedirs(staging, exist_ok=True)
        dest = os.path.join(staging, f"{(hit.md5 or 'dl')}_{job.id}.{_ext_for(hit)}")
        job.status = "downloading"
        db.commit()
        async with sem:
            status = await _resolve_download(fetcher, hit, cfg, dest)
        if status == "ok":
            from . import convert
            usable = convert.ensure_epub(dest)   # mobi/azw3 → epub (no-op for epub/pdf)
            verdict = _import_file(db, usable, cw, job, target_dir)
            _cleanup(dest)
            if usable != dest:
                _cleanup(usable)                  # remove the converted file if it wasn't imported/moved
            if verdict == "imported":
                return
            # A file was obtained but it's wrong / corrupt / unimportable → record it BROKEN so this
            # exact candidate is never retried (a future re-search will look for different ones).
            broken.mark_broken(db, cand, reason=(job.error or "verify/integrity failed")[:200])
        elif status == "throttled":
            # The endpoint is blocked / overloaded / timing out — NOT a dead link. Leave this
            # candidate in place (don't advance, don't blacklist) and re-queue the whole job to retry
            # once the endpoint resolves, backing off so we don't hammer it.
            _cleanup(dest)
            if requeue_on_transient:
                return _requeue_transient(db, job, hit.host or "endpoint")
            job.status = "failed"
            job.error = f"{hit.host or 'endpoint'} blocked/unreachable (transient)"
            db.commit()
            return
        else:  # "fail" — this specific link is terminally dead/wrong; try the next candidate.
            _cleanup(dest)
        job.attempt += 1
        db.commit()
    job.status = "failed"
    job.error = job.error or "no open-library source had a matching, verifiable file"
    db.commit()


def _requeue_transient(db: Session, job: DownloadJob, host: str) -> None:
    """A transient block/timeout fetching `job` → keep it QUEUED and retry with growing backoff, until
    it has failed MAX_TRANSIENT_RETRIES times (then give up). Honoured by the worker's `not_before`
    gate so a backed-off job isn't picked up before its retry time."""
    from datetime import UTC, datetime, timedelta
    job.retries = (job.retries or 0) + 1
    if job.retries > MAX_TRANSIENT_RETRIES:
        job.status = "failed"
        job.not_before = None
        job.error = (f"{host} stayed blocked/unreachable after {MAX_TRANSIENT_RETRIES} retries — "
                     "giving up; it can be re-queued manually")
        db.commit()
        log.info("libgen job %s failed: endpoint %s blocked after %d retries", job.id, host,
                 MAX_TRANSIENT_RETRIES)
        return
    delay = _retry_backoff_s(job.retries)
    job.status = "queued"
    job.not_before = datetime.now(UTC) + timedelta(seconds=delay)
    job.error = (f"{host} blocked/unreachable — queued for retry "
                 f"{job.retries}/{MAX_TRANSIENT_RETRIES} in {int(delay // 60)} min")
    db.commit()
    log.info("libgen job %s requeued: %s blocked, retry %d/%d in %dm", job.id, host, job.retries,
             MAX_TRANSIENT_RETRIES, int(delay // 60))


def _cleanup(path: str) -> None:
    for p in (path, path + ".part"):
        try:
            if os.path.isfile(p):
                os.remove(p)
        except OSError:
            pass


def _sweep_staging(target_dir: str | None, *, grace_s: float = 2 * 3600) -> int:
    """GC orphaned partials in the libgen staging dir left by a process killed mid-download (normal
    completions are _cleanup'd inline). Only files older than ``grace_s`` are removed, so an in-flight
    download is never touched. Without this the staging dir — which lives INSIDE the watched library
    dir — slowly accumulates partials that folder-sync may then try to import. Returns count removed."""
    if not target_dir:
        return 0
    staging = os.path.join(target_dir, ".openlib-staging")
    if not os.path.isdir(staging):
        return 0
    import time
    cutoff = time.time() - grace_s
    removed = 0
    try:
        names = os.listdir(staging)
    except OSError:
        return 0
    for name in names:
        p = os.path.join(staging, name)
        try:
            if os.path.isfile(p) and os.path.getmtime(p) < cutoff:
                os.remove(p)
                removed += 1
        except OSError:
            pass
    return removed


async def libgen_tick() -> dict:
    """Worker: advance queued/downloading libgen jobs (bounded per tick). No-op unless configured."""
    from ..db import SessionLocal
    db = SessionLocal()
    fetcher = None
    try:
        integ = get_integration(db)
        if integ is None:
            return {"skipped": "not configured"}
        cfg = load_config(integ)
        target_dir = _target_dir(db, cfg)
        from datetime import UTC, datetime
        from sqlalchemy import or_
        now = datetime.now(UTC)
        jobs = db.scalars(
            select(DownloadJob).where(
                DownloadJob.grab_kind == KIND,
                DownloadJob.status.in_(("queued", "downloading")),
                # A transient-retry job backed off to a future time waits its turn.
                or_(DownloadJob.not_before.is_(None), DownloadJob.not_before <= now),
            ).order_by(DownloadJob.id).limit(PER_TICK)
        ).all()
        if not jobs:
            return {"active": 0}
        if not target_dir:
            # A real destination is required — never import into (and then auto-watch) a temp dir.
            for job in jobs:
                job.status = "failed"
                job.error = ("no download directory configured — set one on the Open Libraries "
                             "integration, or configure the SABnzbd library path")
            db.commit()
            return {"failed": len(jobs), "error": "no download_dir"}
        _sweep_staging(target_dir)   # GC partials from any process killed mid-download
        fetcher = Fetcher(cfg)
        for job in jobs:
            try:
                await _advance_job(db, job, cfg, fetcher, target_dir)
            except Exception:  # noqa: BLE001 — one bad job must not stall the queue
                db.rollback()
                job.status, job.error = "failed", "libgen processing error"
                db.commit()
                log.exception("libgen job %s failed", job.id)
        return {"processed": len(jobs)}
    finally:
        if fetcher is not None:
            await fetcher.aclose()
        db.close()


async def test_connection(integ: Integration) -> dict:
    """Reachability check for the integrations UI: is at least one enabled provider host answering?"""
    cfg = load_config(integ)
    fetcher = Fetcher(cfg)
    try:
        for host in cfg.libgen_hosts:
            html = await fetcher.get_html(f"https://{host}/index.php",
                                          params={"req": "test", "objects[]": "f"})
            if html:
                return {"ok": True, "app": "Open libraries (LibGen)",
                        "detail": f"reachable via {host} · providers: {', '.join(cfg.providers)}"}
        return {"ok": False, "error": "no configured LibGen mirror is reachable right now"}
    finally:
        await fetcher.aclose()
