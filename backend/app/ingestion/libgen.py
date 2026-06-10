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

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import CatalogWork, DownloadJob, Integration, Work
from . import verify
from .extract import authors_compatible, norm_title

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

# Conservative defaults — the operator can loosen/tighten per integration.
DEFAULT_PROVIDERS = ["libgen", "annas"]            # the robust ones on by default
ALL_PROVIDERS = ["libgen", "annas", "zlibrary", "oceanofpdf", "liber3"]
DEFAULT_LIBGEN_HOSTS = ["libgen.la", "libgen.gl", "libgen.bz", "libgen.vg", "libgen.li"]
DEFAULT_MIN_INTERVAL_S = 2.0          # min seconds between requests to the SAME host
DEFAULT_MAX_PER_DAY = 300             # per-host daily request cap
DEFAULT_MAX_CONCURRENT = 2           # global cap on concurrent downloads
DEFAULT_FORMATS = ["epub", "pdf"]    # only formats the importer can ingest
CANDIDATE_CAP = 8                     # most candidates we'll download+verify before giving up
SEARCH_LIMIT = 25                     # results parsed per provider search
PER_TICK = 3                          # jobs advanced per worker tick (download regulation)


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
    global _concurrency, _conc_limit
    if _concurrency is None or limit != _conc_limit:
        _concurrency, _conc_limit = asyncio.Semaphore(limit), limit
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
    st = _HOSTS.setdefault(host, _HostState())
    st.blocked_until = time.monotonic() + max(1.0, retry_after)


_CF_CHALLENGE_MARKERS = (b"just a moment", b"challenge-platform", b"cf-chl", b"cf-browser-verification",
                         b"_cf_chl", b"turnstile")


def _is_cf_challenge(resp: httpx.Response, body: bytes) -> bool:
    """A Cloudflare ANTI-BOT challenge (worth a browser retry), as opposed to a plain origin error
    (e.g. an overloaded nginx 503, which the browser can't fix)."""
    if (resp.headers.get("cf-mitigated") or "").lower() == "challenge":
        return True
    return any(m in (body[:4000].lower()) for m in _CF_CHALLENGE_MARKERS)


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
            self._client = httpx.AsyncClient(timeout=30.0, follow_redirects=True, headers=_HTML_HEADERS)
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
            return getattr(page, "html", None)
        except Exception as exc:  # noqa: BLE001 — render is best-effort
            log.info("libgen render %s failed: %s", url, exc)
            return None

    async def download(self, url: str, dest: str, *, render_host: str | None = None,
                       referer: str | None = None) -> str:
        """Stream `url` to `dest`. Returns "ok" (file written), "blocked" (a Cloudflare anti-bot
        CHALLENGE — worth retrying through the headless browser), or "fail" (origin error / wrong
        content / not a file — don't waste a browser attempt). Reuses browser clearance cookies when
        `render_host` is set."""
        host = _host_of(url)
        try:
            await _throttle(host, self.cfg)
        except RateLimitExceeded:
            return "fail"
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
                    with open(tmp, "wb") as fh:
                        async for chunk in r.aiter_bytes(65536):
                            fh.write(chunk)
                    os.replace(tmp, dest)
                    return "ok" if os.path.getsize(dest) > 1024 else "fail"
                if r.status_code in (429, 503):
                    _note_backoff(host, _retry_after_seconds(r))
                body = await r.aread()
                if _is_cf_challenge(r, body):
                    return "blocked"
                log.info("libgen download %s → HTTP %s (origin)", url, r.status_code)
                return "fail"
        except httpx.HTTPError as exc:
            log.info("libgen download %s failed: %s", url, exc)
            return "fail"
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
    return bool(ext) and ext.lower() in cfg.formats


def _score_hit(cw: CatalogWork, h: Hit) -> float:
    """Title-recall (token overlap) gated by author compatibility — same spirit as the usenet matcher,
    enough to rank candidates for download+verify (verify.py is the real precision gate)."""
    want = set(norm_title(cw.title or "").split())
    got = set(norm_title(h.title or "").split())
    if not want or not got:
        return 0.0
    recall = len(want & got) / len(want)
    if cw.author and h.author and not authors_compatible(cw.author, h.author):
        recall *= 0.4
    return recall


def candidates_for(cw: CatalogWork, hits: list[Hit], cfg: Config) -> list[Hit]:
    """Rank + cap the importable-format hits for a book (best title/author match first)."""
    scored = [(h, _score_hit(cw, h)) for h in hits if _good_format(h.ext, cfg)]
    scored = [(h, s) for h, s in scored if s >= 0.5]
    scored.sort(key=lambda hs: hs[1], reverse=True)
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
    parts = [cw.title or ""]
    if cw.author:
        parts.append(cw.author.split(",")[0].split(";")[0])
    return " ".join(p for p in parts if p).strip()


async def _libgen_search(fetcher: Fetcher, cfg: Config, cw: CatalogWork) -> list[Hit]:
    """Search the LibGen mirror family (first host that answers; they share one database)."""
    from bs4 import BeautifulSoup
    q = _libgen_query(cw)
    for host in cfg.libgen_hosts:
        html = await fetcher.get_html(
            f"https://{host}/index.php",
            params={"req": q, "columns[]": "t", "objects[]": "f", "res": str(SEARCH_LIMIT)},
        )
        if not html:
            continue
        soup = BeautifulSoup(html, "html.parser")
        table = soup.find("table", id="tablelibgen") or soup.find("table", class_=re.compile("table"))
        if table is None:
            continue
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
            title = " ".join(tds[0].get_text(" ", strip=True).split())
            ext = (tds[7].get_text(strip=True) or "").lower() or None
            yr = re.search(r"(\d{4})", tds[3].get_text(" ", strip=True))
            hits.append(Hit(
                provider="libgen", title=title,
                author=(tds[1].get_text(", ", strip=True) or None),
                ext=ext, size=_parse_size(tds[6].get_text(" ", strip=True)),
                year=int(yr.group(1)) if yr else None,
                language=(tds[4].get_text(strip=True) or None),
                md5=md5, host=host, page_url=f"https://{host}/ads.php?md5={md5}", direct_url=None,
            ))
        if hits:
            return hits
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
async def _annas_search(fetcher: Fetcher, cfg: Config, cw: CatalogWork) -> list[Hit]:
    """Anna's Archive search → MD5s (downloaded via the LibGen mirrors, which share the MD5 space)."""
    from bs4 import BeautifulSoup
    html = await fetcher.get_html("https://annas-archive.gl/search", params={"q": _libgen_query(cw)})
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    hits: list[Hit] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=re.compile(r"/md5/[a-fA-F0-9]{32}")):
        m = re.search(r"/md5/([a-fA-F0-9]{32})", a["href"])
        if not m or m.group(1) in seen:
            continue
        md5 = m.group(1).lower()
        seen.add(md5)
        text = " ".join(a.get_text(" ", strip=True).split())
        # Anna's puts "ext, lang, size · Title · Author" style metadata in the card text.
        ext = None
        em = re.search(r"\b(epub|pdf|mobi|azw3|cbz|cbr|fb2|djvu)\b", text, re.I)
        if em:
            ext = em.group(1).lower()
        hits.append(Hit(
            provider="annas", title=text[:300], author=None, ext=ext,
            size=_parse_size(text), year=None, language=None,
            md5=md5, host=None, page_url=f"https://annas-archive.gl/md5/{md5}", direct_url=None,
        ))
        if len(hits) >= SEARCH_LIMIT:
            break
    return hits


# --------------------------------------------------------------- providers: render-based (best-effort)
async def _render_search(fetcher: Fetcher, provider: str, url: str, params: dict | None = None) -> str | None:
    full = url
    if params:
        from urllib.parse import urlencode
        full = f"{url}?{urlencode(params)}"
    return await fetcher.get_html(full, render=True)


async def _zlibrary_search(fetcher: Fetcher, cfg: Config, cw: CatalogWork) -> list[Hit]:
    """z-library.sk via the headless browser. Download needs an account (optional creds); without
    one the candidates are still surfaced but the download step will fall through to other providers."""
    from bs4 import BeautifulSoup
    from urllib.parse import quote
    html = await fetcher.get_html(f"https://z-library.sk/s/{quote(_libgen_query(cw))}", render=True)
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


async def _oceanofpdf_search(fetcher: Fetcher, cfg: Config, cw: CatalogWork) -> list[Hit]:
    """oceanofpdf.com via the headless browser (Cloudflare). Best-effort."""
    from bs4 import BeautifulSoup
    html = await _render_search(fetcher, "oceanofpdf", "https://oceanofpdf.com/", {"s": _libgen_query(cw)})
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


async def _liber3_search(fetcher: Fetcher, cfg: Config, cw: CatalogWork) -> list[Hit]:
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
async def search_book(db: Session, cw: CatalogWork, cfg: Config, fetcher: Fetcher) -> list[Hit]:
    """Run the enabled providers (in config order) and return ranked, importable-format candidates."""
    all_hits: list[Hit] = []
    for prov in cfg.providers:
        fn = _PROVIDERS.get(prov)
        if fn is None:
            continue
        try:
            hits = await fn(fetcher, cfg, cw)
            log.info("libgen search %s %r → %d hits", prov, cw.title, len(hits))
            all_hits.extend(hits)
        except Exception:  # noqa: BLE001 — one provider must not abort the search
            log.exception("libgen provider %s search failed", prov)
    return candidates_for(cw, all_hits, cfg)


async def _resolve_download(fetcher: Fetcher, hit: Hit, cfg: Config, dest: str) -> bool:
    """Download one candidate to `dest`. LibGen + Anna's resolve via the LibGen ads→get flow (shared
    MD5s); render providers download from their page's direct link with browser cookies."""
    render_host = hit.host if hit.provider in ("zlibrary", "oceanofpdf") else None
    if hit.direct_url:
        return await _fetch_with_fallback(fetcher, hit.direct_url, dest, referer=hit.page_url,
                                          render_host=render_host)
    # LibGen + Anna's: resolve the MD5 through a LibGen mirror.
    if hit.md5:
        hosts = [hit.host] if hit.host else []
        hosts += [h for h in cfg.libgen_hosts if h not in hosts]
        for host in hosts:
            got = await _libgen_get_url(fetcher, host, hit.md5)
            if got is None:
                continue
            url, referer = got
            if await _fetch_with_fallback(fetcher, url, dest, referer=referer):
                return True
    # Render providers (z-library / OceanOfPDF): render the book page, pull a download link, fetch it.
    if render_host and hit.page_url:
        url = await _extract_download_link(fetcher, hit)
        if url:
            return await _fetch_with_fallback(fetcher, url, dest, referer=hit.page_url,
                                              render_host=render_host)
    return False


async def _fetch_with_fallback(fetcher: Fetcher, url: str, dest: str, *, referer: str | None = None,
                               render_host: str | None = None) -> bool:
    """Plain HTTP first; only if the host answers with a Cloudflare CHALLENGE (not a plain origin
    error like an overloaded 503) do we spend a headless-browser attempt to solve it."""
    status = await fetcher.download(url, dest, referer=referer, render_host=render_host)
    if status == "ok":
        return True
    if status == "blocked":
        return await fetcher.download_via_browser(url, dest, referer=referer)
    return False


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


def _import_file(db: Session, path: str, cw: CatalogWork, job: DownloadJob,
                 target_dir: str | None) -> str:
    """Verify a downloaded file and, on success, promote + import it into the library and link it to
    the catalog book + requester. Returns 'imported' | 'retry' | 'failed' and sets job.status."""
    from . import downloads as dl
    from ..library import add_to_library
    from .local_folder import sync_folder

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
        "direct_url": h.direct_url, "key": h.key(),
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
        await _advance_job(db, job, cfg, fetcher, stock_dir)
        db.refresh(job)
        return job
    finally:
        await fetcher.aclose()


def _hit_from_cand(c: dict) -> Hit:
    return Hit(provider=c.get("provider", "libgen"), title=c.get("title") or "",
               author=c.get("author"), ext=c.get("ext"), size=c.get("size"), year=None,
               language=None, md5=c.get("md5"), host=c.get("host"),
               page_url=c.get("page_url"), direct_url=c.get("direct_url"))


async def _advance_job(db: Session, job: DownloadJob, cfg: Config, fetcher: Fetcher,
                       target_dir: str | None) -> None:
    """Download + verify the job's current candidate; on failure advance to the next; import on
    success. Caps each job at CANDIDATE_CAP attempts."""
    cw = db.get(CatalogWork, job.catalog_work_id) if job.catalog_work_id else None
    if cw is None:
        job.status, job.error = "failed", "catalog entry no longer exists"
        db.commit()
        return
    cands = job.candidates or []
    sem = _semaphore(cfg.max_concurrent)
    while job.attempt < len(cands) and job.attempt < CANDIDATE_CAP:
        cand = cands[job.attempt]
        hit = _hit_from_cand(cand)
        staging = os.path.join(
            (target_dir or tempfile.gettempdir()), ".openlib-staging")
        os.makedirs(staging, exist_ok=True)
        dest = os.path.join(staging, f"{(hit.md5 or 'dl')}_{job.id}.{_ext_for(hit)}")
        job.status = "downloading"
        db.commit()
        async with sem:
            ok = await _resolve_download(fetcher, hit, cfg, dest)
        if ok:
            verdict = _import_file(db, dest, cw, job, target_dir)
            if verdict == "imported":
                _cleanup(dest)
                return
            # verify failed → drop the file and try the next candidate
            _cleanup(dest)
        else:
            _cleanup(dest)
        job.attempt += 1
        db.commit()
    job.status = "failed"
    job.error = job.error or "no open-library source had a matching, verifiable file"
    db.commit()


def _cleanup(path: str) -> None:
    for p in (path, path + ".part"):
        try:
            if os.path.isfile(p):
                os.remove(p)
        except OSError:
            pass


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
        jobs = db.scalars(
            select(DownloadJob).where(
                DownloadJob.grab_kind == KIND,
                DownloadJob.status.in_(("queued", "downloading")),
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
