"""Cloudflare challenge solver via a FlareSolverr-compatible proxy.

Some sources (e.g. comix.to) sit behind a Cloudflare interstitial / Turnstile challenge that a plain
HTTP client — and even the in-app headless renderer — can't pass. FlareSolverr (run by the operator,
configured via ``SHELF_FLARESOLVERR_URL``) drives a real, evasion-hardened browser to solve the
challenge and returns the page HTML, the cookies it earned (``cf_clearance``, ``__cf_bm``, …) and the
exact ``User-Agent`` it used.

cf_clearance is bound to (client IP, User-Agent), so we DON'T proxy every request through the solver
(it's slow + serializes on one browser). Instead we solve ONCE per host, cache the cookies + UA, and
replay them on cheap plain-HTTP requests until they expire — re-solving only when a challenge recurs.

Solver note: solving a JSON-API URL directly tends to time out (FlareSolverr waits for an HTML
challenge page to clear, but the API returns JSON). So ``ensure_clearance`` always solves the SITE
ROOT (an HTML page); the earned cf_clearance is domain-wide and lets the caller hit the API directly.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx

from ..config import get_settings

log = logging.getLogger("shelf.flaresolverr")

# Cookies worth replaying on plain HTTP — the Cloudflare clearance/bot-management set.
_CF_COOKIES = {"cf_clearance", "__cf_bm", "__cflb", "cf_chl_rc_m"}


@dataclass
class Solution:
    """A solved page from the proxy."""
    status: int
    html: str
    cookies: list[dict] = field(default_factory=list)   # raw [{name,value,domain,path,...}]
    user_agent: str = ""


@dataclass
class Clearance:
    """Cached Cloudflare clearance for one host: the cookies + the UA they're bound to."""
    cookies: dict[str, str]
    user_agent: str
    ts: float

    def cookie_header(self) -> str:
        return "; ".join(f"{k}={v}" for k, v in self.cookies.items())


# host -> Clearance, plus a per-host lock so concurrent crawlers don't all solve the same host at once.
_clearance: dict[str, Clearance] = {}
_locks: dict[str, asyncio.Lock] = {}
# host -> monotonic time of the last FAILED solve. Solving is slow (tens of seconds) and a host whose
# challenge the solver can't pass (e.g. a Turnstile the proxy version doesn't support) would otherwise
# cost a full solver timeout on EVERY page. After a failure we back off re-solving that host for a
# window so a crawl fails fast instead of stalling on the solver repeatedly.
_solve_failed_at: dict[str, float] = {}
_FAIL_COOLDOWN_S = 600.0


def _endpoint() -> str | None:
    """The FlareSolverr ``/v1`` URL, or None when unconfigured."""
    base = (get_settings().flaresolverr_url or "").strip().rstrip("/")
    if not base:
        return None
    return base if base.endswith("/v1") else f"{base}/v1"


def configured() -> bool:
    return _endpoint() is not None


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def _ttl() -> float:
    # The pydantic field already defaults to 1500 when unset, so read it directly (a literal 0 means
    # "don't reuse" — re-solve on every challenge — not "fall back to the default").
    return max(0.0, float(get_settings().flaresolverr_clearance_ttl_s))


async def solve(url: str, *, timeout_s: float | None = None) -> Solution | None:
    """Drive the proxy to fetch ``url`` past any Cloudflare challenge. Returns the Solution, or None
    on any failure (unconfigured, transport error, solver error/timeout). Never raises."""
    ep = _endpoint()
    if not ep:
        return None
    t = float(timeout_s if timeout_s is not None else get_settings().flaresolverr_timeout_s)
    payload = {"cmd": "request.get", "url": url, "maxTimeout": int(t * 1000)}
    try:
        # The proxy is an operator-trusted internal service (commonly a private IP), so — like the
        # Prowlarr/SABnzbd clients — it is NOT routed through the public-only SSRF guard. We give the
        # HTTP call headroom over the solver's own maxTimeout so we read the result rather than racing it.
        async with httpx.AsyncClient(timeout=t + 20) as client:
            r = await client.post(ep, json=payload)
    except httpx.HTTPError as exc:
        log.warning("flaresolverr unreachable at %s: %s", ep, exc)
        return None
    if r.status_code != 200:
        log.warning("flaresolverr HTTP %s solving %s", r.status_code, url)
        return None
    try:
        data = r.json()
    except ValueError:
        return None
    if (data.get("status") or "").lower() != "ok":
        log.info("flaresolverr could not solve %s: %s", url, (data.get("message") or "")[:160])
        return None
    sol = data.get("solution") or {}
    return Solution(
        status=int(sol.get("status") or 0),
        html=sol.get("response") or "",
        cookies=[c for c in (sol.get("cookies") or []) if c.get("name")],
        user_agent=sol.get("userAgent") or "",
    )


def clearance_for(url: str) -> Clearance | None:
    """A FRESH cached clearance for ``url``'s host, or None when absent/expired."""
    cl = _clearance.get(_host(url))
    if cl and (time.time() - cl.ts) < _ttl():
        return cl
    return None


async def ensure_clearance(url: str, *, force: bool = False) -> Clearance | None:
    """Return a fresh clearance for ``url``'s host, solving the site ROOT via the proxy if needed.
    Cached per host + locked so concurrent callers solve at most once. None when unconfigured or the
    solve fails. Best-effort; never raises."""
    host = _host(url)
    if not host or not configured():
        return None
    if not force:
        cur = clearance_for(url)
        if cur:
            return cur
    failed_at = _solve_failed_at.get(host)
    if not force and failed_at is not None and (time.monotonic() - failed_at) < _FAIL_COOLDOWN_S:
        return None   # recently failed to solve this host — fail fast instead of paying the timeout
    lock = _locks.setdefault(host, asyncio.Lock())
    async with lock:
        if not force:
            cur = clearance_for(url)   # another waiter may have just solved it
            if cur:
                return cur
            failed_at = _solve_failed_at.get(host)
            if failed_at is not None and (time.monotonic() - failed_at) < _FAIL_COOLDOWN_S:
                return None
        parts = urlsplit(url)
        root = f"{parts.scheme or 'https'}://{host}/"
        sol = await solve(root)
        jar = {c["name"]: c.get("value", "") for c in (sol.cookies if sol else [])
               if c["name"] in _CF_COOKIES}
        if sol is None or "cf_clearance" not in jar:
            # Solve failed / cleared no cookie → back off re-solving this host for a while.
            _solve_failed_at[host] = time.monotonic()
            if sol is not None:
                log.info("flaresolverr solved %s but returned no cf_clearance", root)
            return None
        cl = Clearance(cookies=jar, user_agent=sol.user_agent, ts=time.time())
        _clearance[host] = cl
        _solve_failed_at.pop(host, None)
        log.info("flaresolverr: cached cf_clearance for %s (UA pinned)", host)
        return cl


def invalidate(url: str) -> None:
    """Drop a host's cached clearance (call when a replayed clearance still gets challenged)."""
    _clearance.pop(_host(url), None)


def clear_all() -> None:
    _clearance.clear()
    _solve_failed_at.clear()
