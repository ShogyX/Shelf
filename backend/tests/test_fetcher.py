"""Stage 5 tests: rate limiting, robots blocking, backoff."""
from __future__ import annotations

import httpx
import pytest

from app.ingestion.fetcher import (
    DailyBudgetExceeded,
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


async def test_daily_budget_exceeded():
    budget = SourceBudget(min_request_interval_s=0.0, max_daily_requests=2)

    async def sleep(_):
        return None

    await budget.acquire(wall_fn=lambda: 0.0, sleep=sleep)
    await budget.acquire(wall_fn=lambda: 0.0, sleep=sleep)
    with pytest.raises(DailyBudgetExceeded):
        await budget.acquire(wall_fn=lambda: 0.0, sleep=sleep)


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
