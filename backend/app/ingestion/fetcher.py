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
        """Block until this source is allowed to make one more request."""
        async with self._lock:
            wall = wall_fn()
            # Reset the daily counter every 24h window.
            if wall - self._day_start >= 86400:
                self._day_start = wall
                self._requests_today = 0
            if self._requests_today >= self.max_daily_requests:
                raise DailyBudgetExceeded(
                    f"daily budget of {self.max_daily_requests} requests exhausted"
                )
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
            self._requests_today += 1

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
        self._semaphore = asyncio.Semaphore(global_max_concurrency)
        self._client = client
        self._owns_client = client is None
        self._browser = None  # lazy BrowserFetcher

    async def _get_client(self) -> httpx.AsyncClient:
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
    ) -> SourceBudget:
        # Update in place when possible so a settings change doesn't reset rate counters.
        budget = self._budgets.get(source_key)
        if budget is None:
            budget = SourceBudget(
                min_request_interval_s=min_request_interval_s,
                max_daily_requests=max_daily_requests,
                robots_respected=robots_respected,
                render_js=render_js,
            )
            self._budgets[source_key] = budget
        else:
            budget.min_request_interval_s = min_request_interval_s
            budget.max_daily_requests = max_daily_requests
            budget.robots_respected = robots_respected
            budget.render_js = render_js
        return budget

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
        max_retries: int = 4,
    ) -> httpx.Response:
        """Polite GET: robots check -> rate-limit -> request -> backoff on 429/5xx."""
        if not await self.allowed(source_key, url):
            raise RobotsDisallowed(f"robots.txt disallows {url}")

        headers = dict(headers or {})  # extra per-request headers (e.g. auth token)
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified

        budget = self._budget(source_key)
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
    ):
        """Fetch a page as HTML, transparently using the headless browser when the source
        has `render_js` enabled (or ``force_render`` is set — e.g. a Cloudflare-fronted JSON
        API that a plain HTTP client can't reach). Returns an object with `.status_code`,
        `.text`, `.url` and `.raise_for_status()` (httpx.Response or RenderedPage)."""
        if not await self.allowed(source_key, url):
            raise RobotsDisallowed(f"robots.txt disallows {url}")
        if force_render or self._budget(source_key).render_js:
            await asyncio.to_thread(assert_public_url, url)  # SSRF guard for the browser path
            await self._budget(source_key).acquire()
            async with self._semaphore:
                return await self._get_browser().render(
                    url, wait_selector=wait_selector, headers=headers or None
                )
        return await self.get(source_key, url, headers=headers)
