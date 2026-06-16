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
import importlib.util
import logging
import random
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from urllib import robotparser
from urllib.parse import urljoin, urlparse

import httpx
from .. import telemetry

from .netguard import _pin_to_ip, assert_public_url

log = logging.getLogger("shelf.fetcher")

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


def _cookie_header_for(jar, url: str) -> str | None:
    """Build the ``Cookie`` request header that the jar would attach for ``url`` (matching by
    domain/path/secure via stdlib cookielib). Needed because we connect to a pinned IP — the request
    URL's host is the IP, so httpx's automatic, hostname-scoped cookie attachment won't fire."""
    import urllib.request
    req = urllib.request.Request(url)
    try:
        jar.add_cookie_header(req)
    except Exception:  # noqa: BLE001 — never let cookie matching break a fetch
        return None
    return req.get_header("Cookie")


class RobotsDisallowed(Exception):
    """Raised when a path is disallowed by the source's robots.txt."""


class DailyBudgetExceeded(Exception):
    """Raised when a source's max_daily_requests has been spent."""


class RateLimited(Exception):
    """The source is throttling / anti-bot-blocking us right now (HTTP 429, or a Cloudflare
    challenge / block). NOT a per-item failure — every caller should cool the whole job/site down
    and retry later (exponential backoff), not fail the chapter/page and keep hammering the block.
    Detected centrally in the fetcher, so it's UNIVERSAL across every source adapter.

    ``challenge`` distinguishes an anti-bot CHALLENGE (a headless-browser render might pass it) from
    a plain overload/ban (rendering won't help) — drives the plain-HTTP→render auto-escalation."""

    def __init__(self, *args, challenge: bool = False) -> None:
        super().__init__(*args)
        self.challenge = challenge


class CircuitOpen(RateLimited):
    """A per-host circuit breaker is OPEN — the host has failed/blocked repeatedly, so we FAST-FAIL
    new requests instead of parking a coroutine (and a scarce global slot) on its cooldown. A
    RateLimited subclass so every existing caller already cools the job down + retries later; never
    flagged as a challenge, so it does NOT trigger the plain-HTTP→render escalation."""

    def __init__(self, *args) -> None:
        super().__init__(*args, challenge=False)


# Anti-bot block detection: delegated to the SHARED detector (challenge.py) — one marker set,
# full-body scans, 200-status challenges recognized. A 429 is always a rate-limit; a members-only/
# paywalled 403 carries no challenge markers → handled as usual, not a cooldown.
from .challenge import is_challenge as _is_challenge
from .challenge import via_cloudflare as _via_cloudflare


def _looks_blocked(status: int, headers, body) -> bool:
    """True when a response is an anti-bot/Cloudflare block rather than real content. ``body`` is a
    lazy callable; it's only invoked for suspicious statuses or responses that provably transited
    Cloudflare (so a normal 2xx page from a CF-less origin is never scanned)."""
    if status == 429:
        return True  # explicit "Too Many Requests" — always a rate-limit
    if status in (403, 503) or _via_cloudflare(headers) or _header_cf_mitigated(headers):
        try:
            text = body() or ""
        except Exception:  # noqa: BLE001
            text = ""
        return _is_challenge(status, headers, text)
    return False


def _header_cf_mitigated(headers) -> bool:
    try:
        return bool(((headers or {}).get("cf-mitigated") or "").strip())
    except Exception:  # noqa: BLE001 — non-mapping headers
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
    consecutive_blocks: int = 0       # consecutive anti-bot blocks (drives escalating cooldown)
    # Circuit breaker: ANY pushback (block OR transient timeout) counts here; past the threshold the
    # breaker opens and acquire-time callers fast-fail (see circuit_guard).
    consecutive_failures: int = 0
    _circuit_open_until: float = 0.0  # monotonic; while now < this (and tripped) requests fast-fail
    _probing: bool = False            # a single half-open probe is in flight
    _probe_started: float = 0.0       # monotonic; self-heals a stranded probe flag

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
            # RESERVE this request's slot before releasing the lock, so concurrent callers on the
            # same bucket queue one interval apart instead of all reading the same _last_request_ts
            # and stacking their waits additively. We deliberately do NOT hold the lock across the
            # sleep — a long Retry-After cooldown would otherwise block every other caller's slot
            # computation for the full window.
            self._last_request_ts = ready_at
        wait = ready_at - now
        if wait > 0:
            await sleep(wait)

    # Block-ledger cooldowns (E3): a detected ANTI-BOT BLOCK imposes a host-level cooldown that
    # EVERY caller of this source then waits out (via _next_allowed_ts in acquire) — not just a rate
    # nudge — and it ESCALATES on consecutive blocks so we stop hammering a host that's banning us.
    _SOFT_BLOCK_BASE_S = 30.0      # 503/429 overload-style block → short
    _HARD_BLOCK_BASE_S = 120.0     # 403 / Cloudflare challenge / anti-bot → longer
    _BLOCK_COOLDOWN_CAP_S = 3600.0
    # Circuit breaker: open after this many consecutive failures (blocks OR timeouts); the open
    # window grows exponentially past the threshold. Capped at the block-cooldown cap.
    _CIRCUIT_THRESHOLD = 4
    _CIRCUIT_OPEN_BASE_S = 60.0
    _PROBE_TIMEOUT_S = 120.0       # self-heal a half-open probe flag if its request never resolves

    def penalize(self, retry_after: float | None = None, *, block: bool = False,
                 hard: bool = False, now_fn=time.monotonic) -> None:
        """A site pushed back — back our request rate down. With ``block`` (a detected anti-bot
        block, not a mere slow 5xx), also impose an escalating host-level cooldown so repeated
        blocks back off hard (and ``reward`` resets the streak on a clean response). EVERY pushback
        also advances the circuit breaker so a host that keeps failing gets short-circuited."""
        self.throttle_factor = min(self.throttle_factor * 1.7, self.max_throttle_factor)
        self._ok_streak = 0
        now = now_fn()
        if retry_after and retry_after > 0:
            self._next_allowed_ts = max(self._next_allowed_ts, now + retry_after)
        if block:
            self.consecutive_blocks += 1
            base = self._HARD_BLOCK_BASE_S if hard else self._SOFT_BLOCK_BASE_S
            cooldown = min(base * (2 ** (self.consecutive_blocks - 1)), self._BLOCK_COOLDOWN_CAP_S)
            self._next_allowed_ts = max(self._next_allowed_ts, now + cooldown)
        # Breaker bookkeeping (blocks AND transient failures). A failed half-open probe lands here:
        # clearing _probing re-arms the breaker so the NEXT probe waits out a fresh open window.
        self.consecutive_failures += 1
        self._probing = False
        if self.consecutive_failures >= self._CIRCUIT_THRESHOLD:
            over = self.consecutive_failures - self._CIRCUIT_THRESHOLD
            open_s = min(self._CIRCUIT_OPEN_BASE_S * (2 ** over), self._BLOCK_COOLDOWN_CAP_S)
            # Never open for less than an in-force block cooldown.
            self._circuit_open_until = max(self._circuit_open_until, now + open_s,
                                           self._next_allowed_ts)

    def reward(self) -> None:
        """A clean response — clear the block/failure streaks (closing the breaker) and gradually
        relax back toward the configured rate."""
        self._ok_streak += 1
        self.consecutive_blocks = 0   # not blocked right now → reset the escalation
        self.consecutive_failures = 0
        self._circuit_open_until = 0.0
        self._probing = False         # a successful probe closes the breaker
        if self._ok_streak >= 5 and self.throttle_factor > 1.0:
            self.throttle_factor = max(1.0, self.throttle_factor / 1.5)
            self._ok_streak = 0

    def circuit_guard(self, now_fn=time.monotonic) -> None:
        """Fast-fail (raise ``CircuitOpen``) when this host's breaker is OPEN. Once the open window
        elapses, admit exactly ONE half-open probe; concurrent callers keep fast-failing until that
        probe resolves (reward→close, penalize→re-open). Distinct from acquire()'s cooldown WAIT: a
        wedged host short-circuits IMMEDIATELY so it can't park coroutines/global slots while reader
        fetches for healthy hosts keep flowing. Call BEFORE acquire()."""
        if self.consecutive_failures < self._CIRCUIT_THRESHOLD:
            return
        now = now_fn()
        if now < self._circuit_open_until:
            raise CircuitOpen("circuit open — host failing repeatedly")
        # Open window elapsed → half-open. Let one probe through; others fast-fail until it resolves
        # (or the probe is presumed lost after _PROBE_TIMEOUT_S, which re-admits one).
        if self._probing and (now - self._probe_started) < self._PROBE_TIMEOUT_S:
            raise CircuitOpen("circuit open — probe in flight")
        self._probing = True
        self._probe_started = now


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
        # Per-host fairness: each rate bucket gets its own tiny semaphore so ONE slow/blocked host
        # can't hold every global slot and starve the rest (13B). Acquired BEFORE the global cap so a
        # host waiting on its own slot never parks a scarce global one.
        self._host_semaphores: dict[str, asyncio.Semaphore] = {}
        self._host_concurrency = 1
        self._client = client
        self._owns_client = client is None
        self._browser = None  # lazy BrowserFetcher
        self._stale_clients: list[httpx.AsyncClient] = []  # superseded clients, closed lazily
        # Per-host sticky CHALLENGE solver: once a host needs a stronger tier ('render' or 'zendriver')
        # to get past Cloudflare, remember it so the next fetch goes straight there instead of
        # re-failing the cheaper tiers. Lets the app auto-adapt when a site newly adds/escalates CF.
        self._host_solver: dict[str, str] = {}

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
        """Resize the global fetch concurrency cap at runtime by adjusting the EXISTING semaphore in
        place — never by replacing it. Replacing the object would let in-flight fetches (still holding
        slots on the old one) plus the new cap's slots run concurrently, transiently exceeding the cap.
        Raising adds permits; lowering parks the surplus permits (acquired and held) so the new lower
        cap is enforced as in-flight fetches finish."""
        n = max(1, int(n))
        if n == self._concurrency:
            return
        delta = n - self._concurrency
        self._concurrency = n
        sem = self._semaphore
        if delta > 0:
            for _ in range(delta):           # plain (non-bounded) Semaphore: release adds permits
                sem.release()
        else:
            async def _shrink(k: int) -> None:
                for _ in range(k):           # remove permits by acquiring + never releasing them
                    await sem.acquire()
            try:
                asyncio.get_running_loop().create_task(_shrink(-delta))
            except RuntimeError:
                # No running loop → no in-flight fetches; rebuild at the new size is safe here.
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
                # Transport wrapper counts every crawl request with its outcome (success / blocked /
                # timeout / error) — including ones that raise before a response (timeouts).
                transport=telemetry.async_transport("crawl"),
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
            # Persist clearance cookies under backend/state so they survive restarts (13B).
            state_path = Path(__file__).resolve().parents[2] / "state" / "browser_state.json"
            self._browser = BrowserFetcher(user_agent=ua, storage_state_path=state_path)
        return self._browser

    @staticmethod
    def _browser_usable() -> bool:
        """Whether a headless browser CAN run (the optional 'render' extra is installed). Checked
        before auto-escalating a challenge to a render so a host isn't stuck render_js=True on an
        install with no Playwright."""
        return importlib.util.find_spec("playwright") is not None

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
                # Also clear the block ledger + circuit breaker so a manually-resumed source isn't
                # left fast-failing on a stale open breaker.
                budget.consecutive_blocks = 0
                budget.consecutive_failures = 0
                budget._circuit_open_until = 0.0
                budget._probing = False
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

    @staticmethod
    def _bucket_key(source_key: str, rate_key: str | None) -> str:
        """The key identifying this request's host/bucket — matches what ``_rate_budget`` paces on,
        so the per-host semaphore and the rate budget cover the SAME host."""
        return rate_key if (rate_key and rate_key != source_key) else source_key

    def _host_sem(self, bucket_key: str) -> asyncio.Semaphore:
        sem = self._host_semaphores.get(bucket_key)
        if sem is None:
            sem = asyncio.Semaphore(self._host_concurrency)
            self._host_semaphores[bucket_key] = sem
        return sem

    @asynccontextmanager
    async def _slot(self, bucket_key: str):
        """Acquire a network slot: the per-host semaphore FIRST (fairness), then the global cap.
        Order matters — taking the global cap first would let a host with all its per-host slots
        busy still pin global slots and starve other hosts."""
        async with self._host_sem(bucket_key):
            async with self._semaphore:
                yield

    async def _absorb_clearance(self, url: str) -> None:
        """After a successful render, copy the browser's clearance cookies (cf_clearance, __cf_bm, …)
        for this host into the plain-HTTP client's jar so subsequent plain GETs skip the challenge,
        and persist the browser storage_state so clearance survives a restart (13B). Best-effort."""
        if self._browser is None:
            return
        try:
            cookies = await self._browser.cookies_for(url)
        except Exception:  # noqa: BLE001
            cookies = []
        if cookies:
            try:
                client = await self._get_client()
                for c in cookies:
                    name = c.get("name")
                    if not name:
                        continue
                    client.cookies.set(
                        name, c.get("value") or "",
                        domain=(c.get("domain") or "").lstrip("."),
                        path=c.get("path") or "/",
                    )
            except Exception:  # noqa: BLE001
                pass
        try:
            await self._browser.persist_state()
        except Exception:  # noqa: BLE001
            pass

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
        bucket_key = self._bucket_key(source_key, rate_key)
        budget.circuit_guard()  # fast-fail a host whose breaker is open (before any wait/slot)
        cur = url
        redirects = 0
        while True:
            # SSRF guard: re-validate the target on EVERY hop (a permitted host can redirect
            # to an internal one). DNS resolution is blocking → run it off the event loop. We
            # PIN the connection to one of the IPs validated here (DNS-rebinding-safe, S6): a name
            # that resolves public for the check but internal at connect time can no longer slip
            # through. assert_public_url returns those IPs precisely so we can pin.
            ips = await asyncio.to_thread(assert_public_url, cur)
            pinned_url, host_hdr, ext = _pin_to_ip(cur, ips[0])
            attempt = 0
            while True:
                attempt += 1
                await budget.acquire()
                try:
                    async with self._slot(bucket_key):
                        client = await self._get_client()
                        # The request URL's host is now the IP, so the client cookie jar (keyed by
                        # the real hostname — e.g. cf_clearance) won't auto-attach; build the Cookie
                        # header for the ORIGINAL url from the jar and send it explicitly.
                        req_headers = {**headers, **host_hdr}
                        cookie_hdr = _cookie_header_for(client.cookies.jar, cur)
                        if cookie_hdr:
                            req_headers.setdefault("Cookie", cookie_hdr)
                        resp = await client.get(pinned_url, headers=req_headers, extensions=ext)
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

            # Follow redirects manually so each hop passes the SSRF guard + robots check. Resolve
            # the next hop against the ORIGINAL hostname url (`cur`), NOT resp.url — resp.url now
            # carries the pinned IP, so a relative redirect must still resolve against the real host.
            if resp.is_redirect and redirects < 5:
                loc = resp.headers.get("location")
                if loc:
                    cur = urljoin(cur, loc)
                    redirects += 1
                    if not await self.allowed(source_key, cur):
                        raise RobotsDisallowed(f"robots.txt disallows {cur}")
                    continue
            # An anti-bot / Cloudflare block (or a 429 that survived every retry) means the source is
            # blocking us — surface it as RateLimited so the caller cools the whole job/site down and
            # retries later, instead of hammering the block. (A members-only/paywalled 403 has no
            # block markers, so it falls through and is handled normally below.)
            if _looks_blocked(resp.status_code, getattr(resp, "headers", {}), lambda: resp.text):
                # An anti-bot BLOCK → escalating host cooldown (every caller waits), hard for a 403/
                # challenge, softer for a 429/503 overload.
                budget.penalize(_parse_retry_after(resp.headers.get("Retry-After")),
                                block=True, hard=(resp.status_code not in (429, 503)))
                # Flag whether this is a solvable CHALLENGE (so get_html can escalate to a render)
                # vs a plain overload/ban (where a render wouldn't help).
                is_chal = _is_challenge(resp.status_code, getattr(resp, "headers", {}), resp.text)
                raise RateLimited(f"{source_key}: blocked at {cur} (HTTP {resp.status_code})",
                                  challenge=is_chal)
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
        etag: str | None = None,
        last_modified: str | None = None,
    ):
        """Fetch a page as HTML, transparently using the headless browser when the source
        has `render_js` enabled (or ``force_render`` is set — e.g. a Cloudflare-fronted JSON
        API that a plain HTTP client can't reach). Returns an object with `.status_code`,
        `.text`, `.url` and `.raise_for_status()` (httpx.Response or RenderedPage).

        ``rate_key`` selects an independent rate-limit bucket (see ``get``)."""
        if not await self.allowed(source_key, url):
            raise RobotsDisallowed(f"robots.txt disallows {url}")
        budget = self._budget(source_key)
        bucket = self._bucket_key(source_key, rate_key)
        rk = dict(wait_selector=wait_selector, headers=headers, scroll=scroll,
                  rate_key=rate_key, max_retries=max_retries)

        # Sticky fast-path: a host already known to need the strongest tier skips the cheaper ones.
        if self._host_solver.get(bucket) == "zendriver":
            zr = await self._zendriver_render(source_key, url, headers=headers, rate_key=rate_key)
            if zr is not None:
                return zr
            # solver currently can't pass it (cooldown/unavailable) — fall through to render/plain.
        if force_render or budget.render_js or self._host_solver.get(bucket) == "render":
            rendered = await self._render(source_key, url, **rk)
            # Even a forced/sticky render escalates to zendriver when the result is STILL a challenge
            # (Turnstile defeats headless Playwright) — _result_is_challenge is precise, so a normal
            # rendered page is never mis-escalated.
            if self._result_is_challenge(rendered):
                zr = await self._zendriver_render(source_key, url, headers=headers, rate_key=rate_key)
                if zr is not None:
                    return zr
            return rendered

        # Tier ladder: plain HTTP → FlareSolverr clearance replay → in-app render → zendriver. Each
        # tier is tried only on a CHALLENGE, in cost order, and the WINNER is remembered per host so a
        # site that newly adds (or escalates) Cloudflare is handled automatically next time.
        try:
            # Conditional GET only on the plain-HTTP tier (render/solver tiers can't 304). An
            # unchanged page returns a bodyless 304 the caller skips re-parsing (F04).
            return await self.get(source_key, url, headers=headers, rate_key=rate_key,
                                  etag=etag, last_modified=last_modified)
        except RateLimited as exc:
            if not exc.challenge:
                raise
            # Tier 1: external FlareSolverr — earns clearance we replay cheaply over plain HTTP.
            solved = await self._solver_retry(source_key, url, headers=headers, rate_key=rate_key)
            if solved is not None and not self._result_is_challenge(solved):
                return solved
            # Tier 2: in-app headless render (sticky on the budget so this host renders directly next).
            if self._browser_usable():
                log.info("%s: challenge on plain HTTP — escalating to browser render", source_key)
                budget.render_js = True
                rendered = await self._render(source_key, url, **rk)
                if not self._result_is_challenge(rendered):
                    return rendered
                log.info("%s: render still challenged — escalating to zendriver", source_key)
            # Tier 3: headful zendriver (Turnstile-capable). Sticky on success.
            zr = await self._zendriver_render(source_key, url, headers=headers, rate_key=rate_key)
            if zr is not None:
                return zr
            raise

    @staticmethod
    def _result_is_challenge(resp) -> bool:
        """True when a fetched/rendered/solved response is STILL a Cloudflare challenge (so the caller
        escalates to a stronger tier instead of returning the interstitial as if it were content).

        Robust against a SOLVED page that merely still embeds the CF /challenge-platform/ script:
        ``looks_like_challenge_page`` only fires on a SHORT, image-less interstitial, so a real
        (long, image-rich) cleared page is never mis-flagged."""
        from .challenge import is_challenge, looks_like_challenge_page
        status = getattr(resp, "original_status", None) or getattr(resp, "status_code", 200)
        headers = getattr(resp, "headers", {}) or {}
        body = getattr(resp, "text", "") or getattr(resp, "body_text", "") or ""
        if status in (403, 429, 503) and is_challenge(status, headers, body):
            return True
        return looks_like_challenge_page(body)

    async def _zendriver_render(self, source_key: str, url: str, *,
                                headers: dict[str, str] | None, rate_key: str | None):
        """Strongest tier: solve ``url`` in a headful zendriver subprocess (passes Turnstile). On
        success, inject its clearance cookies + UA into the plain-HTTP jar, mark the host sticky
        'zendriver', and return a RenderedPage built from the solved HTML. None to fall back."""
        from . import zendriver_solver
        from .browser import RenderedPage
        if not zendriver_solver.available():
            return None
        try:
            await asyncio.to_thread(assert_public_url, url)  # SSRF guard for the browser path
        except Exception:  # noqa: BLE001
            return None
        budget = self._rate_budget(source_key, rate_key)
        bucket = self._bucket_key(source_key, rate_key)
        await budget.acquire()
        try:
            data = await zendriver_solver.solve(url)
        except Exception:  # noqa: BLE001 — solver is best-effort, never break the crawl
            data = None
        if not data:
            budget.penalize()
            telemetry.record(urlparse(url).hostname, "solver", "error")  # solver couldn't fetch it
            return None
        page = RenderedPage(status=int(data.get("status") or 200),
                            text=data.get("html") or "",
                            url=url, body_text=data.get("body_text") or "")
        challenged = self._result_is_challenge(page)
        telemetry.record(urlparse(url).hostname, "solver", "blocked" if challenged else "success")
        if challenged:
            budget.penalize()
            return None
        # Replay the earned clearance + UA on subsequent cheap plain GETs of this host. Adopt the UA
        # FIRST — set_identity rebuilds the client on a UA change, so cookies must be injected into
        # the client that survives that rebuild, not one that's about to be discarded.
        try:
            host = (urlparse(url).hostname or "")
            if data.get("user_agent"):
                self.set_identity(data["user_agent"], self.contact_email)
            client = await self._get_client()
            for c in data.get("cookies") or []:
                if c.get("name"):
                    client.cookies.set(c["name"], c.get("value") or "",
                                       domain=(c.get("domain") or host).lstrip("."), path="/")
        except Exception:  # noqa: BLE001
            pass
        self._host_solver[bucket] = "zendriver"
        budget.reward()
        log.info("%s: passed Cloudflare via zendriver (sticky)", source_key)
        return page

    async def _solver_retry(self, source_key: str, url: str, *,
                            headers: dict[str, str] | None, rate_key: str | None):
        """Earn Cloudflare clearance from the configured FlareSolverr proxy, inject it into the plain-
        HTTP jar (and adopt the solver's UA — cf_clearance is UA-bound), then retry the plain GET.
        Returns the response on success, or None to fall back to the in-app render. Never raises."""
        from . import flaresolverr
        if not flaresolverr.configured():
            return None
        try:
            cl = await flaresolverr.ensure_clearance(url)
        except Exception:  # noqa: BLE001 — solver is best-effort, never break the crawl
            return None
        if cl is None:
            return None
        try:
            host = (urlparse(url).hostname or "")
            if cl.user_agent:
                # Pin the solver's UA so subsequent plain GETs keep matching cf_clearance. Do this
                # BEFORE injecting the cookies: set_identity rebuilds the client when the UA changes,
                # so cookies set first would land on a client that's immediately discarded and the
                # replay would re-challenge.
                self.set_identity(cl.user_agent, self.contact_email)
            client = await self._get_client()
            for name, value in cl.cookies.items():
                client.cookies.set(name, value, domain=host, path="/")
            log.info("%s: passed Cloudflare via FlareSolverr — replaying clearance over plain HTTP",
                     source_key)
            return await self.get(source_key, url, headers=headers, rate_key=rate_key)
        except RateLimited:
            flaresolverr.invalidate(url)   # replayed clearance still challenged → drop it
            return None

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
        bucket_key = self._bucket_key(source_key, rate_key)
        budget.circuit_guard()
        await budget.acquire()
        try:
            async with self._slot(bucket_key):
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
        host = urlparse(url).hostname  # a headless render counts as one "crawl" fetch — recorded
        budget = self._rate_budget(source_key, rate_key)        # with its real OUTCOME below, never
        bucket_key = self._bucket_key(source_key, rate_key)     # pre-counted as success before it runs
        budget.circuit_guard()
        attempt = 0
        while True:
            attempt += 1
            await budget.acquire()
            try:
                async with self._slot(bucket_key):
                    page = await self._get_browser().render(
                        url, wait_selector=wait_selector, headers=headers or None, scroll=scroll
                    )
            except Exception:  # navigation timeout / browser crash — transient, back off + retry
                budget.penalize()
                if attempt > max_retries:
                    telemetry.record(host, "crawl", "error")
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
            # Headers are consulted ONLY when the browser did NOT clear the challenge (status
            # still suspicious): the nav-response headers describe the PRE-clearance interstitial,
            # so cf-mitigated on a successfully-cleared render (status downgraded to 200 after a
            # full-body marker check) must not re-flag it as blocked. The full rendered body is
            # always scanned.
            page_headers = getattr(page, "headers", {}) if status != 200 else {}
            page_body = getattr(page, "text", "") or ""
            blocked = _looks_blocked(status, page_headers, lambda: page_body)
            if not blocked and status == 200:
                # A challenge served with native HTTP 200 (e.g. the passive wait timed out and
                # the interstitial is still up): STRUCTURAL markers in the rendered body convict;
                # text markers alone never do at 200, so real prose is never flagged.
                blocked = _is_challenge(200, {}, page_body)
            if blocked:
                budget.penalize(block=True, hard=(status not in (429, 503)))
                telemetry.record(host, "crawl", "blocked")
                raise RateLimited(f"{source_key}: blocked at {url} (HTTP {status})")
            if status >= 400:
                budget.penalize()
                telemetry.record(host, "crawl", "error")
            else:
                budget.reward()
                telemetry.record(host, "crawl", "success")
                # Hand the browser-earned clearance to the plain-HTTP client + persist it (13B): a
                # successful render means we just passed the challenge, so capture cf_clearance now.
                await self._absorb_clearance(url)
            return page
