"""Tests for the FlareSolverr Cloudflare-solver integration + the comix API clearance replay."""
from __future__ import annotations

import asyncio

import pytest

from app.config import get_settings
from app.ingestion import comix_catalog as cc, flaresolverr


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    flaresolverr.clear_all()
    # Neutralize any ambient .env (a real deployment sets SHELF_FLARESOLVERR_URL) so every test starts
    # UNCONFIGURED unless it opts in via _enable() — an env var (even empty) beats the .env file.
    monkeypatch.setenv("SHELF_FLARESOLVERR_URL", "")
    get_settings.cache_clear()
    yield
    flaresolverr.clear_all()
    get_settings.cache_clear()


def _enable(monkeypatch, url="http://solver.local:8191"):
    monkeypatch.setenv("SHELF_FLARESOLVERR_URL", url)
    get_settings.cache_clear()


def test_configured_reflects_setting(monkeypatch):
    get_settings.cache_clear()
    assert flaresolverr.configured() is False
    _enable(monkeypatch)
    assert flaresolverr.configured() is True
    # endpoint normalizes to /v1
    assert flaresolverr._endpoint() == "http://solver.local:8191/v1"
    _enable(monkeypatch, "http://solver.local:8191/v1")
    assert flaresolverr._endpoint() == "http://solver.local:8191/v1"


def test_ensure_clearance_caches_and_reuses(monkeypatch):
    _enable(monkeypatch)
    calls = {"n": 0}

    async def fake_solve(url, **k):
        calls["n"] += 1
        return flaresolverr.Solution(
            status=200, html="<html/>",
            cookies=[{"name": "cf_clearance", "value": "TOKEN"}, {"name": "junk", "value": "x"}],
            user_agent="UA/Test")

    monkeypatch.setattr(flaresolverr, "solve", fake_solve)

    async def go():
        a = await flaresolverr.ensure_clearance("https://comix.to/api/v1/manga?page=1")
        b = await flaresolverr.ensure_clearance("https://comix.to/other")  # same host → cached
        return a, b

    a, b = asyncio.run(go())
    assert a is b
    assert calls["n"] == 1                      # solved ONCE, reused for the host
    assert a.cookies == {"cf_clearance": "TOKEN"}  # only CF cookies kept
    assert a.user_agent == "UA/Test"
    assert flaresolverr.clearance_for("https://comix.to/x") is a


def test_ensure_clearance_none_without_cf_clearance(monkeypatch):
    _enable(monkeypatch)

    async def fake_solve(url, **k):
        return flaresolverr.Solution(status=200, html="", cookies=[{"name": "__cf_bm", "value": "y"}],
                                     user_agent="UA")

    monkeypatch.setattr(flaresolverr, "solve", fake_solve)
    assert asyncio.run(flaresolverr.ensure_clearance("https://x.com/")) is None


def test_failed_solve_backs_off(monkeypatch):
    """A host the solver can't pass must NOT be re-attempted on every call (each solve is slow)."""
    _enable(monkeypatch)
    calls = {"n": 0}

    async def fake_solve(url, **k):
        calls["n"] += 1
        return None   # solver can't pass this challenge (e.g. comix Turnstile)

    monkeypatch.setattr(flaresolverr, "solve", fake_solve)

    async def go():
        a = await flaresolverr.ensure_clearance("https://hard.com/")
        b = await flaresolverr.ensure_clearance("https://hard.com/page2")  # within cooldown
        return a, b

    a, b = asyncio.run(go())
    assert a is None and b is None
    assert calls["n"] == 1   # solved once, then backed off (no second slow attempt)


def test_clearance_ttl_expiry(monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setenv("SHELF_FLARESOLVERR_CLEARANCE_TTL_S", "0")
    get_settings.cache_clear()

    async def fake_solve(url, **k):
        return flaresolverr.Solution(status=200, html="", user_agent="UA",
                                     cookies=[{"name": "cf_clearance", "value": "T"}])

    monkeypatch.setattr(flaresolverr, "solve", fake_solve)
    asyncio.run(flaresolverr.ensure_clearance("https://x.com/"))
    assert flaresolverr.clearance_for("https://x.com/") is None  # ttl 0 → immediately stale


def test_unconfigured_is_a_noop(monkeypatch):
    get_settings.cache_clear()
    assert asyncio.run(flaresolverr.ensure_clearance("https://x.com/")) is None
    assert asyncio.run(flaresolverr.solve("https://x.com/")) is None


# ----------------------------------------------------------- comix API clearance replay
class _Resp:
    def __init__(self, status, headers, text, js=None):
        self.status_code, self.headers, self._text, self._js = status, headers, text, js

    @property
    def text(self):
        return self._text

    def json(self):
        if self._js is None:
            raise ValueError("not json")
        return self._js


def test_comix_fetch_solves_challenge_then_replays_clearance(monkeypatch):
    _enable(monkeypatch)
    import app.ingestion.netguard as ng
    monkeypatch.setattr(ng, "assert_public_url", lambda u: None)

    async def fake_solve(url, **k):
        return flaresolverr.Solution(status=200, html="<html/>", user_agent="UA/9",
                                     cookies=[{"name": "cf_clearance", "value": "XYZ"}])

    monkeypatch.setattr(flaresolverr, "solve", fake_solve)

    seen = {"n": 0}

    async def fake_api_get(url):
        seen["n"] += 1
        cl = flaresolverr.clearance_for(url)
        if cl is None:                                  # first try → Cloudflare challenge
            return _Resp(403, {"cf-mitigated": "challenge"}, "Just a moment...")
        assert cl.cookies["cf_clearance"] == "XYZ" and cl.user_agent == "UA/9"
        return _Resp(200, {}, "{}", js={"result": {"items": [], "meta": {}}})

    monkeypatch.setattr(cc, "_api_get", fake_api_get)
    result = asyncio.run(cc._fetch_page(1))
    assert result == {"items": [], "meta": {}}
    assert seen["n"] == 2                                # challenged, then retried with clearance


def test_comix_fetch_returns_none_when_solver_absent(monkeypatch):
    get_settings.cache_clear()                          # solver NOT configured
    import app.ingestion.netguard as ng
    monkeypatch.setattr(ng, "assert_public_url", lambda u: None)

    async def fake_api_get(url):
        return _Resp(403, {"cf-mitigated": "challenge"}, "Just a moment...")

    monkeypatch.setattr(cc, "_api_get", fake_api_get)
    assert asyncio.run(cc._fetch_page(1)) is None       # graceful: no solver → just fails
