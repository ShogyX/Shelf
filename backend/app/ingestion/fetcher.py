"""PoliteFetcher (Stage 5).

A polite async HTTP client that, per source:
  * reads & caches robots.txt, refusing disallowed paths,
  * enforces a token-bucket rate limit (min interval + daily cap) with a random
    per-request jitter so pauses are never machine-regular,
  * adaptively *scales itself down* when a site pushes back: 429/503/timeouts grow a
    per-source throttle factor (and honour Retry-After); sustained success decays it,
  * retries transient timeouts and dropped connections with jittered exponential backoff,
  * sends an honest, identifying User-Agent and supports conditional GET.

A hard global concurrency cap is enforced across all sources.
"""
from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from urllib import robotparser
from urllib.parse import urljoin, urlparse

import httpx

from .netguard import assert_public_url

# Transport-level failures we treat as transient (retry with backoff + self-throttle).
TRANSIENT_ERRORS = (
    httpx.TimeoutException,   # connect/read/write/pool timeouts
    httpx.ConnectError,       # connection refused / DNS / TLS reset
    httpx.ReadError,          # connection dropped mid-read
    httpx.WriteError,
    httpx.RemoteProtocolError,
)


def _parse_retry_after(value: str | None) -> float | None:
    """Parse a Retry-After header (delta-seconds; HTTP-date form is treated as absent)."""
    if not value:
        return None
    try:
        secs = float(value)
        return secs if secs > 0 else None
    except ValueError:
        return None  # HTTP-date form — fall back to our own backoff


class RobotsDisallowed(Exception):
    """Raised when a path is disallowed by the source's robots.txt."""


class DailyBudgetExceeded(Exception):
    """Raised when a source's max_daily_requests has been spent."""


class RateLimited(Exception):
    """The source is throttling / anti-bot-blocking us right now (HTTP 429, or a Cloudflare
    challenge / block). NOT a per-item failure — every caller should cool the whole job/site down
    and retry later (exponential backoff), not fail the chapter/page and keep hammering the block.
    Detected centrally in the fetcher, so it's UNIVERSAL across every source adapter."""


# Cloudflare / generic anti-bot challenge markers. A 429 is always a rate-limit; a 403/503 (or the
# ``cf-mitigated`` response header) carrying one of these is an anti-bot BLOCK, not a normal
# 'forbidden' (members-only/paywalled 403s carry none of these → handled as usual, not a cooldown).
_BLOCK_MARKERS = (
    "attention required", "you have been blocked", "checking your browser", "just a moment",
    "__cf_chl", "cf-mitigated", "cf-error-details", "cloudflare ray id", "ddos protection by",
    "captcha-delivery", "access denied", "request blocked",
)


def _looks_blocked(status: int, headers, body) -> bool:
    """True when a response is an anti-bot/Cloudflare block rather than real content. ``body`` is a
    lazy callable so a normal (2xx) page's body is never scanned — only suspicious statuses are."""
    if status == 429:
        return True  # explicit "Too Many Requests" — always a rate-limit
    try:
        if ((headers or {}).get("cf-mitigated") or "").strip():
            return True  # Cloudflare sets this on a challenge/block response
    except Exception:  # noqa: BLE001 — non-mapping headers
        pass
    if status in (403, 503):
        try:
            text = (body() or "")[:4096].lower()
        except Exception:  # noqa: BLE001
            text = ""
        return any(m in text for m in _BLOCK_MARKERS)
    return False


@dataclass
class SourceBudget:
    min_request_interval_s: float
    max_daily_requests: int
    robots_respected: bool = True
    render_js: bool = False

    # Adaptive politeness knobs.
    jitter_frac: float = 0.4          # add 0..jitter_frac of the interval as random delay
    throttle_factor: float = 1.0      # multiplies the interval; grows under pushback
    max_throttle_factor: float = 16.0
    _ok_streak: int = 0               # consecutive successes (drives decay)

    _last_request_ts: float = 0.0
    _next_allowed_ts: float = 0.0     # honour Retry-After / cooldowns (monotonic clock)
    _day_start: float = field(default_factory=lambda: 0.0)
    _requests_today: int = 0
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def effective_interval(self, rand=random.random) -> float:
        """Base interval scaled by the adaptive throttle factor, plus random jitter."""
        base = self.min_request_interval_s * self.throttle_factor
        return base + base * self.jitter_frac * rand()

    async def acquire(
        self, now_fn=time.monotonic, wall_fn=time.time, sleep=asyncio.sleep, rand=random.random
    ) -> None:
        """Block until this source is allowed to make one more request. There is NO daily request
        cap: gathering is paced only by the polite per-source interval (+ adaptive backoff under
        pushback / Retry-After). (``wall_fn`` is kept for signature compatibility.)"""
        async with self._lock:
            now = now_fn()
            # Earliest we may fire: a jittered interval after the last request, but never
            # before any Retry-After / backoff cooldown the site asked us to observe.
            ready_at = max(
                self._last_request_ts + self.effective_interval(rand), self._next_allowed_ts
            )
            wait = ready_at - now
            if wait > 0:
                await sleep(wait)
            self._last_request_ts = now_fn()

    def penalize(self, retry_after: float | None = None, now_fn=time.monotonic) -> None:
        """A site pushed back (429/503/timeout) — back our request rate down."""
        self.throttle_factor = min(self.throttle_factor * 1.7, self.max_throttle_factor)
        self._ok_streak = 0
        if retry_after and retry_after > 0:
            self._next_allowed_ts = max(self._next_allowed_ts, now_fn() + retry_after)

    def reward(self) -> None:
        """A clean response — gradually relax back toward the configured rate."""
        self._ok_streak += 1
        if self._ok_streak >= 5 and self.throttle_factor > 1.0:
            self.throttle_factor = max(1.0, self.throttle_factor / 1.5)
            self._ok_streak = 0


@dataclass
class _RobotsCache:
    parser: robotparser.RobotFileParser
    fetched_at: float


class PoliteFetcher:
    def __init__(
        self,
        user_agent: str,
        contact_email: str,
        global_max_concurrency: int = 2,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.user_agent = user_agent
        self.contact_email = contact_email
        self._budgets: dict[str, SourceBudget] = {}
        self._robots: dict[str, _RobotsCache] = {}
        self._robots_ttl = 3600.0
        self._concurrency = max(1, global_max_concurrency)
        self._semaphore = asyncio.Semaphore(self._concurrency)
        self._client = client
        self._owns_client = client is None
        self._browser = None  # lazy BrowserFetcher
        self._stale_clients: list[httpx.AsyncClient] = []  # superseded clients, closed lazily

    def set_identity(self, user_agent: str, contact_email: str) -> None:
        """Update the crawl identity (User-Agent + From contact) at runtime. The HTTP client is
        rebuilt lazily, so the next fetch carries the new headers (the old client is closed then);
        a started headless browser adopts the new UA on its next context."""
        ua = (user_agent or "").strip() or self.user_agent
        email = (contact_email or "").strip() or self.contact_email
        if ua == self.user_agent and email == self.contact_email:
            return
        self.user_agent = ua
        self.contact_email = email
        if self._owns_client and self._client is not None:
            self._stale_clients.append(self._client)
            self._client = None
        if self._browser is not None:
            self._browser.user_agent = ua

    def set_concurrency(self, n: int) -> None:
        """Resize the global fetch concurrency cap at runtime. New fetches acquire the new
        semaphore; in-flight fetches drain against the old one (each is short-lived)."""
        n = max(1, int(n))
        if n == self._concurrency:
            return
        self._concurrency = n
        self._semaphore = asyncio.Semaphore(n)

    async def _get_client(self) -> httpx.AsyncClient:
        while self._stale_clients:  # close clients superseded by a live set_identity()
            old = self._stale_clients.pop()
            try:
                await old.aclose()
            except Exception:  # noqa: BLE001
                pass
        if self._client is None:
            self._client = httpx.AsyncClient(
                headers={
                    "User-Agent": self.user_agent,
                    "From": self.contact_email,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                },
                # Redirects are followed MANUALLY (below) so each hop can be re-validated by
                # the SSRF guard — otherwise an allowed host could 302 us to an internal one.
                follow_redirects=False,
                # Granular timeouts: fail fast on connect (so we can retry another),
                # allow slow bodies a longer read window.
                timeout=httpx.Timeout(connect=10.0, read=30.0, write=30.0, pool=10.0),
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None
        if self._browser is not None:
            await self._browser.aclose()
            self._browser = None

    def _get_browser(self):
        if self._browser is None:
            from .browser import BrowserFetcher

            ua = self.user_agent or "Mozilla/5.0"
            self._browser = BrowserFetcher(user_agent=ua)
        return self._browser

    def configure_source(
        self,
        source_key: str,
        min_request_interval_s: float,
        max_daily_requests: int,
        robots_respected: bool = True,
        render_js: bool = False,
        reset_throttle: bool = False,
    ) -> SourceBudget:
        # Update in place so a settings change doesn't reset rate counters. This updates the
        # source's own budget AND every per-domain/per-job bucket DERIVED from it (keys like
        # 'web_index:<domain>'), so an operator change (or reset) reaches every independent crawl.
        if source_key not in self._budgets:
            self._budgets[source_key] = SourceBudget(
                min_request_interval_s=min_request_interval_s,
                max_daily_requests=max_daily_requests,
                robots_respected=robots_respected,
                render_js=render_js,
            )
        prefix = source_key + ":"
        for key, budget in self._budgets.items():
            if key != source_key and not key.startswith(prefix):
                continue
            budget.min_request_interval_s = min_request_interval_s
            budget.max_daily_requests = max_daily_requests
            budget.robots_respected = robots_respected
            budget.render_js = render_js
            if reset_throttle:
                # An explicit operator change ("apply this and continue now"): clear the runtime
                # pacing state so a raised budget / shorter interval takes effect immediately and a
                # bucket stranded on its old spent cap or in adaptive backoff resumes at full speed.
                budget._requests_today = 0
                budget._next_allowed_ts = 0.0
                budget.throttle_factor = 1.0
                budget._ok_streak = 0
        return self._budgets[source_key]

    def _rate_budget(self, source_key: str, rate_key: str | None) -> SourceBudget:
        """The rate-limit bucket that paces this request. Defaults to the source's own budget; a
        ``rate_key`` (e.g. ``'web_index:<domain>'``) gets an INDEPENDENT bucket that inherits the
        source's politeness config — so different domains/jobs each have their own interval,
        adaptive backoff and daily count, and never compete for one shared budget."""
        if not rate_key or rate_key == source_key:
            return self._budget(source_key)
        bucket = self._budgets.get(rate_key)
        if bucket is None:
            base = self._budget(source_key)
            bucket = SourceBudget(
                min_request_interval_s=base.min_request_interval_s,
                max_daily_requests=base.max_daily_requests,
                robots_respected=base.robots_respected,
                render_js=base.render_js,
            )
            self._budgets[rate_key] = bucket
        return bucket

    def is_rendered(self, source_key: str) -> bool:
        return self._budget(source_key).render_js

    @staticmethod
    def _backoff(attempt: int, *, base: float = 1.0, cap: float = 60.0,
                 rand=random.random) -> float:
        """Jittered exponential backoff: base*2^(attempt-1), capped, +0..50% jitter."""
        delay = min(base * (2 ** (attempt - 1)), cap)
        return delay + delay * 0.5 * rand()

    def _budget(self, source_key: str) -> SourceBudget:
        if source_key not in self._budgets:
            # Conservative default if a source was never configured.
            self._budgets[source_key] = SourceBudget(
                min_request_interval_s=5.0, max_daily_requests=500
            )
        return self._budgets[source_key]

    async def _robots_parser(self, url: str) -> robotparser.RobotFileParser:
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        cached = self._robots.get(origin)
        if cached and (time.monotonic() - cached.fetched_at) < self._robots_ttl:
            return cached.parser
        rp = robotparser.RobotFileParser()
        robots_url = urljoin(origin, "/robots.txt")
        try:
            client = await self._get_client()
            resp = await client.get(robots_url)
            if resp.status_code == 200:
                rp.parse(resp.text.splitlines())
            else:
                rp.parse([])  # no robots -> allow all
        except httpx.HTTPError:
            rp.parse([])
        self._robots[origin] = _RobotsCache(parser=rp, fetched_at=time.monotonic())
        return rp

    async def allowed(self, source_key: str, url: str) -> bool:
        budget = self._budget(source_key)
        if not budget.robots_respected:
            return True
        rp = await self._robots_parser(url)
        return rp.can_fetch(self.user_agent, url)

    async def get(
        self,
        source_key: str,
        url: str,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
        headers: dict[str, str] | None = None,
        rate_key: str | None = None,
        max_retries: int = 4,
    ) -> httpx.Response:
        """Polite GET: robots check -> rate-limit -> request -> backoff on 429/5xx.

        ``rate_key`` selects an INDEPENDENT rate-limit bucket (defaults to the source's own); pass
        e.g. ``'web_index:<domain>'`` so each crawled domain is paced + backed-off on its own."""
        if not await self.allowed(source_key, url):
            raise RobotsDisallowed(f"robots.txt disallows {url}")

        headers = dict(headers or {})  # extra per-request headers (e.g. auth token)
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified

        budget = self._rate_budget(source_key, rate_key)
        cur = url
        redirects = 0
        while True:
            # SSRF guard: re-validate the target on EVERY hop (a permitted host can redirect
            # to an internal one). DNS resolution is blocking → run it off the event loop.
            await asyncio.to_thread(assert_public_url, cur)
            attempt = 0
            while True:
                attempt += 1
                await budget.acquire()
                try:
                    async with self._semaphore:
                        client = await self._get_client()
                        resp = await client.get(cur, headers=headers)
                except TRANSIENT_ERRORS:
                    # Timeout or dropped connection: self-throttle and retry, else surface it.
                    budget.penalize()
                    if attempt > max_retries:
                        raise
                    await asyncio.sleep(self._backoff(attempt))
                    continue

                if resp.status_code in (429, 500, 502, 503, 504) and attempt <= max_retries:
                    retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
                    budget.penalize(retry_after)
                    await asyncio.sleep(retry_after if retry_after else self._backoff(attempt))
                    continue
                break

            # Follow redirects manually so each hop passes the SSRF guard + robots check.
            if resp.is_redirect and redirects < 5:
                loc = resp.headers.get("location")
                if loc:
                    cur = urljoin(str(resp.url), loc)
                    redirects += 1
                    if not await self.allowed(source_key, cur):
                        raise RobotsDisallowed(f"robots.txt disallows {cur}")
                    continue
            # An anti-bot / Cloudflare block (or a 429 that survived every retry) means the source is
            # blocking us — surface it as RateLimited so the caller cools the whole job/site down and
            # retries later, instead of hammering the block. (A members-only/paywalled 403 has no
            # block markers, so it falls through and is handled normally below.)
            if _looks_blocked(resp.status_code, getattr(resp, "headers", {}), lambda: resp.text):
                budget.penalize(_parse_retry_after(resp.headers.get("Retry-After")))
                raise RateLimited(f"{source_key}: blocked at {cur} (HTTP {resp.status_code})")
            # A response that reaches here with a pushback status is the site throttling us (a 5xx
            # that survived all retries): back the per-source rate down. Anything else (incl. 404) is
            # clean enough to relax our rate back toward the configured speed.
            if resp.status_code in (403, 500, 502, 503, 504):
                budget.penalize(_parse_retry_after(resp.headers.get("Retry-After")))
            else:
                budget.reward()
            return resp

    async def get_html(
        self,
        source_key: str,
        url: str,
        *,
        wait_selector: str | None = None,
        headers: dict[str, str] | None = None,
        force_render: bool = False,
        scroll: int = 0,
        rate_key: str | None = None,
        max_retries: int = 3,
    ):
        """Fetch a page as HTML, transparently using the headless browser when the source
        has `render_js` enabled (or ``force_render`` is set — e.g. a Cloudflare-fronted JSON
        API that a plain HTTP client can't reach). Returns an object with `.status_code`,
        `.text`, `.url` and `.raise_for_status()` (httpx.Response or RenderedPage).

        ``rate_key`` selects an independent rate-limit bucket (see ``get``)."""
        if not await self.allowed(source_key, url):
            raise RobotsDisallowed(f"robots.txt disallows {url}")
        if force_render or self._budget(source_key).render_js:
            return await self._render(
                source_key, url, wait_selector=wait_selector, headers=headers,
                scroll=scroll, rate_key=rate_key, max_retries=max_retries,
            )
        return await self.get(source_key, url, headers=headers, rate_key=rate_key)

    async def capture_canvas(
        self, source_key: str, url: str, *, want: set[int] | None = None,
        stop_after: int | None = None, rate_key: str | None = None,
    ) -> tuple[int, dict[int, bytes]]:
        """Render a reader page and screenshot its descrambled <canvas> pages, with the same
        politeness as a normal render (robots check, SSRF guard, rate budget, concurrency cap).
        Returns ``(total_pages, {page_index: PNG bytes})``. Used by the descramble job."""
        if not await self.allowed(source_key, url):
            raise RobotsDisallowed(f"robots.txt disallows {url}")
        await asyncio.to_thread(assert_public_url, url)
        budget = self._rate_budget(source_key, rate_key)
        await budget.acquire()
        try:
            async with self._semaphore:
                result = await self._get_browser().capture_canvas_pages(
                    url, want=want, stop_after=stop_after
                )
            budget.reward()
            return result
        except Exception:
            budget.penalize()
            raise

    async def _render(
        self, source_key: str, url: str, *, wait_selector: str | None,
        headers: dict[str, str] | None, max_retries: int, scroll: int = 0,
        rate_key: str | None = None,
    ):
        """Headless-render with the SAME politeness the plain HTTP path has: rate-limit, then
        retry transient failures (navigation timeouts, browser hiccups, 5xx/429 from a
        Cloudflare-fronted origin) with self-throttling backoff. Without this a brief block
        surfaced straight to the caller and got a chapter permanently marked 'failed'."""
        await asyncio.to_thread(assert_public_url, url)  # SSRF guard for the browser path
        budget = self._rate_budget(source_key, rate_key)
        attempt = 0
        while True:
            attempt += 1
            await budget.acquire()
            try:
                async with self._semaphore:
                    page = await self._get_browser().render(
                        url, wait_selector=wait_selector, headers=headers or None, scroll=scroll
                    )
            except Exception:  # navigation timeout / browser crash — transient, back off + retry
                budget.penalize()
                if attempt > max_retries:
                    raise
                await asyncio.sleep(self._backoff(attempt))
                continue
            status = getattr(page, "status_code", 200)
            # A transient pushback status that survived the challenge wait: retry rather than
            # let the caller fail the chapter. Genuine 4xx (401/403/404/418) fall through.
            if status in (429, 500, 502, 503, 504) and attempt <= max_retries:
                budget.penalize()
                await asyncio.sleep(self._backoff(attempt))
                continue
            # An anti-bot / Cloudflare block (a 403/503 challenge, cf-mitigated, or a 429 that
            # outlasted every retry): re-rendering now just deepens the block, so cool the whole
            # job/site down instead — UNIVERSAL across every source that renders.
            if _looks_blocked(status, getattr(page, "headers", {}), lambda: getattr(page, "text", "")):
                budget.penalize()
                raise RateLimited(f"{source_key}: blocked at {url} (HTTP {status})")
            if status >= 400:
                budget.penalize()
            else:
                budget.reward()
            return page
