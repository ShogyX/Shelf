"""Stage 5 tests: rate limiting, robots blocking, backoff."""
from __future__ import annotations

import httpx
import pytest

from app.ingestion.fetcher import (
    PoliteFetcher,
    RateLimited,
    RobotsDisallowed,
    SourceBudget,
)


async def test_rate_limiter_waits_min_interval():
    budget = SourceBudget(min_request_interval_s=0.5, max_daily_requests=100)
    slept: list[float] = []

    fake_now = {"t": 0.0}

    def now_fn():
        return fake_now["t"]

    async def sleep(d):
        slept.append(d)
        fake_now["t"] += d  # simulate time passing

    no_jitter = lambda: 0.0  # noqa: E731 — deterministic interval for the assert
    # First acquire: no wait. Second immediately after: must wait ~interval.
    await budget.acquire(now_fn=now_fn, wall_fn=lambda: 0.0, sleep=sleep, rand=no_jitter)
    await budget.acquire(now_fn=now_fn, wall_fn=lambda: 0.0, sleep=sleep, rand=no_jitter)
    assert slept and abs(slept[0] - 0.5) < 1e-6


async def test_jitter_makes_pauses_vary():
    budget = SourceBudget(min_request_interval_s=1.0, max_daily_requests=100, jitter_frac=0.5)
    # Jitter adds 0..0.5*interval; different rand values -> different pauses.
    assert budget.effective_interval(rand=lambda: 0.0) == 1.0
    assert budget.effective_interval(rand=lambda: 1.0) == 1.5
    assert 1.0 < budget.effective_interval(rand=lambda: 0.5) < 1.5


async def test_adaptive_throttle_grows_and_decays():
    budget = SourceBudget(min_request_interval_s=2.0, max_daily_requests=100)
    assert budget.throttle_factor == 1.0
    # A few pushbacks scale the interval up.
    budget.penalize()
    budget.penalize()
    assert budget.throttle_factor > 1.0
    assert budget.effective_interval(rand=lambda: 0.0) > 2.0
    # Retry-After installs a hard cooldown floor.
    budget.penalize(retry_after=30.0, now_fn=lambda: 0.0)
    assert budget._next_allowed_ts >= 30.0
    # Sustained success decays it back toward the configured rate.
    high = budget.throttle_factor
    for _ in range(5):
        budget.reward()
    assert budget.throttle_factor < high


async def test_block_ledger_escalates_and_resets():
    """E3: a detected anti-bot block imposes a host-level cooldown that escalates on consecutive
    blocks and resets on a clean response."""
    b = SourceBudget(min_request_interval_s=1.0, max_daily_requests=0)
    now = 1000.0
    nf = lambda: now
    b.penalize(block=True, hard=True, now_fn=nf)
    c1 = b._next_allowed_ts - now
    b.penalize(block=True, hard=True, now_fn=nf)
    c2 = b._next_allowed_ts - now
    assert b.consecutive_blocks == 2 and c2 > c1            # escalates (doubling)
    assert c1 >= 120 and c2 >= 240
    b.reward()
    assert b.consecutive_blocks == 0                        # clean response clears the streak
    # a soft (overload) block uses the shorter base
    b2 = SourceBudget(min_request_interval_s=1.0, max_daily_requests=0)
    b2.penalize(block=True, hard=False, now_fn=nf)
    assert 25 <= (b2._next_allowed_ts - now) <= 35
    # a plain (non-block) penalize doesn't touch the BLOCK ledger (but does the failure ledger)
    b3 = SourceBudget(min_request_interval_s=1.0, max_daily_requests=0)
    b3.penalize()
    assert b3.consecutive_blocks == 0 and b3.consecutive_failures == 1


async def test_circuit_breaker_opens_then_half_open_probe():
    """13B: after K consecutive failures (blocks OR timeouts) the breaker OPENS and circuit_guard
    fast-fails; once the open window elapses exactly one half-open probe is admitted; a failed probe
    re-opens (longer), a successful one closes it."""
    from app.ingestion.fetcher import CircuitOpen

    b = SourceBudget(min_request_interval_s=0.0, max_daily_requests=0)
    t = 1000.0
    nf = lambda: t  # noqa: E731
    k = SourceBudget._CIRCUIT_THRESHOLD
    # Below threshold the guard is a no-op (plain timeouts count toward the breaker).
    for _ in range(k - 1):
        b.penalize(now_fn=nf)
    b.circuit_guard(now_fn=nf)                       # still closed
    # Crossing the threshold opens the breaker → fast-fail.
    b.penalize(now_fn=nf)
    assert b.consecutive_failures == k
    with pytest.raises(CircuitOpen):
        b.circuit_guard(now_fn=nf)
    # After the open window: ONE probe admitted, concurrent callers still fast-fail.
    t2 = b._circuit_open_until + 1
    b.circuit_guard(now_fn=lambda: t2)               # admits the half-open probe (no raise)
    with pytest.raises(CircuitOpen):
        b.circuit_guard(now_fn=lambda: t2)           # probe already in flight
    # Probe FAILS → breaker re-opens for a longer window.
    open_before = b._circuit_open_until
    b.penalize(now_fn=lambda: t2)
    assert b._circuit_open_until > open_before
    with pytest.raises(CircuitOpen):
        b.circuit_guard(now_fn=lambda: t2)
    # A later probe SUCCEEDS → breaker closes.
    t3 = b._circuit_open_until + 1
    b.circuit_guard(now_fn=lambda: t3)               # probe admitted
    b.reward()
    assert b.consecutive_failures == 0
    b.circuit_guard(now_fn=lambda: t3)               # closed → no raise


async def test_per_host_semaphore_isolates_buckets():
    """13B: each rate bucket gets its own per-host semaphore (concurrency 1) so the global cap can
    be shared fairly; the bucket key matches what the rate budget paces on."""
    f = PoliteFetcher("t", "t@t", global_max_concurrency=4)
    sem_a = f._host_sem("a")
    assert sem_a is f._host_sem("a")                 # same bucket → same semaphore
    assert sem_a is not f._host_sem("b")             # distinct buckets → independent
    assert sem_a._value == 1                         # per-host concurrency default
    assert f._bucket_key("src", None) == "src"
    assert f._bucket_key("src", "src") == "src"
    assert f._bucket_key("src", "web_index:d.com") == "web_index:d.com"


async def test_slot_serializes_same_host_but_shares_global_cap():
    """13B: _slot serializes requests to ONE host (so a slow host can't pin both global slots) while
    letting DISTINCT hosts run up to the global cap concurrently — i.e. no starvation, no deadlock."""
    import asyncio

    f = PoliteFetcher("t", "t@t", global_max_concurrency=2)
    state = {"global": 0, "gpeak": 0, "apeak": 0, "a": 0}

    async def work(host: str):
        async with f._slot(host):
            state["global"] += 1
            state["gpeak"] = max(state["gpeak"], state["global"])
            if host == "a":
                state["a"] += 1
                state["apeak"] = max(state["apeak"], state["a"])
            await asyncio.sleep(0.05)
            if host == "a":
                state["a"] -= 1
            state["global"] -= 1

    await asyncio.gather(work("a"), work("a"), work("b"), work("b"))
    assert state["apeak"] == 1     # same host serialized
    assert state["gpeak"] == 2     # two distinct hosts ran concurrently under the global cap


async def test_absorb_clearance_copies_browser_cookies_into_http_jar():
    """13B: after a render, the browser's clearance cookies (cf_clearance) are copied into the
    plain-HTTP client's jar so later plain GETs aren't re-challenged, and state is persisted."""
    f = PoliteFetcher("t", "t@t")

    class FakeBrowser:
        persisted = False

        async def cookies_for(self, url):
            return [{"name": "cf_clearance", "value": "XYZ",
                     "domain": ".example.com", "path": "/"}]

        async def persist_state(self):
            FakeBrowser.persisted = True

        async def aclose(self):
            return None

    f._browser = FakeBrowser()
    await f._absorb_clearance("https://www.example.com/page")
    client = await f._get_client()
    assert any(c.name == "cf_clearance" and c.value == "XYZ" for c in client.cookies.jar)
    assert FakeBrowser.persisted
    await f.aclose()


async def test_browser_persist_state_writes_atomically_and_debounces(tmp_path):
    """13B: storage_state is written atomically to disk so clearance survives a restart, and a
    second call with an unchanged cookie set is debounced (no spurious rewrite)."""
    import json

    from app.ingestion.browser import BrowserFetcher

    path = tmp_path / "state" / "browser_state.json"
    bf = BrowserFetcher("ua", storage_state_path=str(path))

    class FakeContext:
        async def storage_state(self):
            return {"cookies": [{"name": "cf_clearance", "domain": ".x.com", "value": "v1"}]}

    bf._context = FakeContext()
    await bf.persist_state()
    assert path.is_file()
    data = json.loads(path.read_text())
    assert data["cookies"][0]["name"] == "cf_clearance"
    fp = bf._state_fingerprint
    await bf.persist_state()          # unchanged → debounced, fingerprint stable
    assert bf._state_fingerprint == fp


async def test_retries_transient_connection_errors(monkeypatch):
    # Make backoff instant so the test is fast.
    monkeypatch.setattr(PoliteFetcher, "_backoff", staticmethod(lambda *a, **k: 0.0))
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("connection dropped", request=request)
        return httpx.Response(200, text="recovered")

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport, base_url="https://example.com")
    fetcher = PoliteFetcher("TestUA", "t@e.com", client=client)
    fetcher.configure_source("s", min_request_interval_s=0.0, max_daily_requests=100)

    resp = await fetcher.get("s", "https://example.com/x", max_retries=3)
    assert resp.status_code == 200 and calls["n"] == 2
    # The drop should have self-throttled the source.
    assert fetcher._budget("s").throttle_factor > 1.0
    await client.aclose()


async def test_no_daily_cap():
    """There is no daily request budget: gathering is paced only by the interval. Even with a
    positive max_daily_requests set, acquire never raises and never blocks on a daily count."""
    budget = SourceBudget(min_request_interval_s=0.0, max_daily_requests=2)

    async def sleep(_):
        return None

    for _ in range(50):  # an old daily cap of 2 would have blocked after the 2nd
        await budget.acquire(wall_fn=lambda: 0.0, sleep=sleep)


@pytest.mark.asyncio
async def test_get_html_escalates_challenge_to_render(monkeypatch):
    """13B: a plain-HTTP CHALLENGE auto-escalates to a browser render and sticks render_js=True;
    a plain (non-challenge) block re-raises unchanged."""
    from app.ingestion.fetcher import PoliteFetcher, RateLimited

    f = PoliteFetcher(user_agent="t", contact_email="t@t")
    f.configure_source("s", min_request_interval_s=0.0, max_daily_requests=0)
    monkeypatch.setattr(PoliteFetcher, "allowed", lambda self, k, u: _true())
    monkeypatch.setattr(PoliteFetcher, "_browser_usable", staticmethod(lambda: True))

    async def render_stub(self, source_key, url, **kw):
        return "RENDERED"
    monkeypatch.setattr(PoliteFetcher, "_render", render_stub)

    async def blocked_get(self, source_key, url, **kw):
        raise RateLimited(f"{source_key}: blocked", challenge=True)
    monkeypatch.setattr(PoliteFetcher, "get", blocked_get)
    out = await f.get_html("s", "https://x/y")
    assert out == "RENDERED" and f._budget("s").render_js is True   # escalated + sticky

    # a non-challenge block (overload/ban) must NOT escalate — re-raises
    f.configure_source("s2", min_request_interval_s=0.0, max_daily_requests=0)
    async def overload_get(self, source_key, url, **kw):
        raise RateLimited(f"{source_key}: overloaded", challenge=False)
    monkeypatch.setattr(PoliteFetcher, "get", overload_get)
    import pytest as _pt
    with _pt.raises(RateLimited):
        await f.get_html("s2", "https://x/z")
    assert f._budget("s2").render_js is False


async def test_get_html_force_render_block_escalates_to_zendriver(monkeypatch):
    """A Turnstile block in the FORCE-RENDER path must escalate to the headful zendriver tier, not
    propagate. _render RAISES on a block (with challenge unset), so the escalation can't rely on
    inspecting a returned page. Regression: comix.to reader pages 200-but-challenge and used to fail
    the whole hook with RateLimited because force_render never reached the zendriver tier."""
    from app.ingestion.fetcher import PoliteFetcher, RateLimited

    f = PoliteFetcher(user_agent="t", contact_email="t@t")
    f.configure_source("comix", min_request_interval_s=0.0, max_daily_requests=0)
    monkeypatch.setattr(PoliteFetcher, "allowed", lambda self, k, u: _true())

    async def blocked_render(self, source_key, url, **kw):
        raise RateLimited(f"{source_key}: blocked at {url} (HTTP 200)")  # challenge defaults False
    monkeypatch.setattr(PoliteFetcher, "_render", blocked_render)

    zdr_calls: list[str] = []
    async def zendriver_ok(self, source_key, url, **kw):
        zdr_calls.append(url)
        return "ZENDRIVER"
    monkeypatch.setattr(PoliteFetcher, "_zendriver_render", zendriver_ok)
    out = await f.get_html("comix", "https://comix.to/title/31z3-kingdom?page=1", force_render=True)
    assert out == "ZENDRIVER" and len(zdr_calls) == 1   # escalated past the block

    # When zendriver is unavailable (None), the block must STAND — no silent success.
    async def zendriver_none(self, source_key, url, **kw):
        return None
    monkeypatch.setattr(PoliteFetcher, "_zendriver_render", zendriver_none)
    import pytest as _pt
    with _pt.raises(RateLimited):
        await f.get_html("comix", "https://comix.to/title/31z3-kingdom?page=2", force_render=True)


async def _true():
    return True


def test_configure_source_reset_throttle_unsticks_a_spent_budget():
    """Changing a source's budget/interval (reset_throttle=True) clears the runtime pacing so a
    source stranded on its old spent cap / in backoff can continue immediately."""
    from app.ingestion.fetcher import PoliteFetcher

    f = PoliteFetcher(user_agent="t", contact_email="t@t")
    b = f.configure_source("s", min_request_interval_s=1.0, max_daily_requests=5)
    b._requests_today = 5          # spent
    b.throttle_factor = 8.0        # backed off after pushback
    b._next_allowed_ts = 999999.0  # in a cooldown
    f.configure_source("s", min_request_interval_s=0.5, max_daily_requests=0, reset_throttle=True)
    assert b._requests_today == 0 and b.throttle_factor == 1.0 and b._next_allowed_ts == 0.0
    assert b.max_daily_requests == 0  # now unlimited


async def test_robots_blocking():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /private/")
        return httpx.Response(200, text="ok")

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport, base_url="https://example.com")
    fetcher = PoliteFetcher("TestUA", "t@e.com", client=client)
    fetcher.configure_source("s", min_request_interval_s=0.0, max_daily_requests=100)

    assert await fetcher.allowed("s", "https://example.com/public/x") is True
    assert await fetcher.allowed("s", "https://example.com/private/x") is False
    with pytest.raises(RobotsDisallowed):
        await fetcher.get("s", "https://example.com/private/x")
    await client.aclose()


async def test_backoff_on_429_then_success():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, text="slow down")
        return httpx.Response(200, text="finally")

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport, base_url="https://example.com")
    fetcher = PoliteFetcher("TestUA", "t@e.com", client=client)
    fetcher.configure_source("s", min_request_interval_s=0.0, max_daily_requests=100)

    resp = await fetcher.get("s", "https://example.com/x", max_retries=3)
    assert resp.status_code == 200
    assert resp.text == "finally"
    assert calls["n"] == 2  # retried once after the 429
    await client.aclose()


# ---- Headless-render path resilience (Cloudflare-fronted sources like J-Novel) -------------
async def test_render_retries_transient_failure_then_succeeds(monkeypatch):
    """A navigation timeout / browser hiccup on the render path is retried with backoff instead
    of surfacing immediately (which would get the chapter permanently marked 'failed')."""
    from types import SimpleNamespace

    monkeypatch.setattr(PoliteFetcher, "_backoff", staticmethod(lambda *a, **k: 0.0))
    monkeypatch.setattr("app.ingestion.fetcher.assert_public_url", lambda *_a, **_k: None)
    fetcher = PoliteFetcher("UA", "e@e.com")
    fetcher.configure_source("jnovel", min_request_interval_s=0.0, max_daily_requests=100,
                             robots_respected=False, render_js=True)

    class FakeBrowser:
        calls = 0
        async def render(self, url, **kw):
            type(self).calls += 1
            if self.calls < 3:
                raise RuntimeError("navigation timeout")  # transient
            return SimpleNamespace(status_code=200, text="ok", body_text="ok")
    fetcher._browser = FakeBrowser()

    resp = await fetcher.get_html("jnovel", "https://x/api", force_render=True, max_retries=3)
    assert resp.status_code == 200
    assert FakeBrowser.calls == 3  # two failures, then success


async def test_render_retries_5xx_then_gives_up(monkeypatch):
    """A persistent 5xx from a Cloudflare-fronted origin is retried then returned (not raised),
    so the caller decides — it is NOT swallowed into an immediate hard failure."""
    from types import SimpleNamespace

    monkeypatch.setattr(PoliteFetcher, "_backoff", staticmethod(lambda *a, **k: 0.0))
    monkeypatch.setattr("app.ingestion.fetcher.assert_public_url", lambda *_a, **_k: None)
    fetcher = PoliteFetcher("UA", "e@e.com")
    fetcher.configure_source("jnovel", min_request_interval_s=0.0, max_daily_requests=100,
                             robots_respected=False, render_js=True)

    class FakeBrowser:
        calls = 0
        async def render(self, url, **kw):
            type(self).calls += 1
            return SimpleNamespace(status_code=503, text="busy", body_text="")
    fetcher._browser = FakeBrowser()

    resp = await fetcher.get_html("jnovel", "https://x/api", force_render=True, max_retries=2)
    assert resp.status_code == 503
    assert FakeBrowser.calls == 3  # initial + 2 retries
    assert fetcher._budget("jnovel").throttle_factor > 1.0  # pushback self-throttled the source


async def test_render_preserves_auth_status_no_retry(monkeypatch):
    """A genuine 418 (J-Novel members-only) is returned immediately — not retried, not masked —
    so the adapter can classify it as 'unavailable' rather than thrashing the source."""
    from types import SimpleNamespace

    monkeypatch.setattr("app.ingestion.fetcher.assert_public_url", lambda *_a, **_k: None)
    fetcher = PoliteFetcher("UA", "e@e.com")
    fetcher.configure_source("jnovel", min_request_interval_s=0.0, max_daily_requests=100,
                             robots_respected=False, render_js=True)

    class FakeBrowser:
        calls = 0
        async def render(self, url, **kw):
            type(self).calls += 1
            return SimpleNamespace(status_code=418, text="BLITZ", body_text="BLITZ")
    fetcher._browser = FakeBrowser()

    resp = await fetcher.get_html("jnovel", "https://x/api", force_render=True, max_retries=3)
    assert resp.status_code == 418
    assert FakeBrowser.calls == 1  # 4xx auth status is terminal — no retry storm


# ---- Universal anti-bot / Cloudflare block detection (every source, both fetch paths) ----------
async def test_render_raises_ratelimited_on_cloudflare_block(monkeypatch):
    """A Cloudflare block on the render path (403 + challenge body) raises RateLimited — UNIVERSAL,
    so any rendered source (comix, jnovel, web_index, …) cools down instead of failing the item."""
    from types import SimpleNamespace

    monkeypatch.setattr(PoliteFetcher, "_backoff", staticmethod(lambda *a, **k: 0.0))
    monkeypatch.setattr("app.ingestion.fetcher.assert_public_url", lambda *_a, **_k: None)
    fetcher = PoliteFetcher("UA", "e@e.com")
    fetcher.configure_source("anysrc", min_request_interval_s=0.0, max_daily_requests=100,
                             robots_respected=False, render_js=True)

    class FakeBrowser:
        calls = 0
        async def render(self, url, **kw):
            type(self).calls += 1
            return SimpleNamespace(
                status_code=403, body_text="",
                text="<title>Attention Required! | Cloudflare</title>Sorry, you have been blocked")
    fetcher._browser = FakeBrowser()

    with pytest.raises(RateLimited):
        await fetcher.get_html("anysrc", "https://x/page", force_render=True, max_retries=2)
    assert FakeBrowser.calls == 1  # a hard block isn't re-rendered (would only deepen it)
    assert fetcher._budget("anysrc").throttle_factor > 1.0  # cooled the source down


async def test_get_raises_ratelimited_on_persistent_429():
    """A 429 that survives every retry raises RateLimited (so the job/site backs off + retries)."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        calls["n"] += 1
        return httpx.Response(429, headers={"Retry-After": "0"}, text="rate limited")

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport, base_url="https://example.com")
    fetcher = PoliteFetcher("UA", "e@e.com", client=client)
    fetcher.configure_source("s", min_request_interval_s=0.0, max_daily_requests=100)
    with pytest.raises(RateLimited):
        await fetcher.get("s", "https://example.com/x", max_retries=2)
    assert calls["n"] == 3  # retried twice, then surfaced as RateLimited
    await client.aclose()


async def test_get_does_not_ratelimit_a_plain_403():
    """A members-only / paywalled 403 (no anti-bot markers) is NOT a RateLimited block — it's
    returned so the adapter can classify it (e.g. as a permanent 'unavailable')."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(403, text="You must be a member to read this chapter")

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport, base_url="https://example.com")
    fetcher = PoliteFetcher("UA", "e@e.com", client=client)
    fetcher.configure_source("s", min_request_interval_s=0.0, max_daily_requests=100)
    resp = await fetcher.get("s", "https://example.com/x", max_retries=1)
    assert resp.status_code == 403  # returned, not raised
    await client.aclose()


def test_rate_budget_independent_per_key():
    """Different rate_keys (e.g. per crawled domain) get INDEPENDENT budgets that inherit the
    source's config — so one site's adaptive backoff never throttles another's."""
    from app.ingestion.fetcher import PoliteFetcher

    f = PoliteFetcher(user_agent="t", contact_email="t@t")
    f.configure_source("web_index", min_request_interval_s=2.0, max_daily_requests=0)
    a = f._rate_budget("web_index", "web_index:a.com")
    b = f._rate_budget("web_index", "web_index:b.com")
    assert a is not b
    assert a.min_request_interval_s == 2.0 and b.max_daily_requests == 0  # inherited config
    a.penalize()  # a backs off after a block
    assert a.throttle_factor > 1.0 and b.throttle_factor == 1.0  # b is unaffected
    # The source's own budget is also distinct from its derived buckets.
    assert f._rate_budget("web_index", None) is not a


def test_configure_source_propagates_to_derived_buckets():
    """An operator budget/interval change (and reset) reaches every per-domain bucket, not just
    the source's own budget."""
    from app.ingestion.fetcher import PoliteFetcher

    f = PoliteFetcher(user_agent="t", contact_email="t@t")
    f.configure_source("web_index", 2.0, 0)
    a = f._rate_budget("web_index", "web_index:a.com")
    a.throttle_factor = 8.0
    a._requests_today = 50
    f.configure_source("web_index", 1.0, 100, reset_throttle=True)
    assert a.min_request_interval_s == 1.0 and a.max_daily_requests == 100  # propagated
    assert a.throttle_factor == 1.0 and a._requests_today == 0              # reset propagated
