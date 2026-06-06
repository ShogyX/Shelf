"""Stage 5 tests: rate limiting, robots blocking, backoff."""
from __future__ import annotations

import httpx
import pytest

from app.ingestion.fetcher import (
    PoliteFetcher,
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
