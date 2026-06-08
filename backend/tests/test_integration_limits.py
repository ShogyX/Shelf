"""Per-integration request limiting: resolve defaults vs overrides, the throttle spacing, and the
provider-catalog endpoint that drives the Settings UI."""
from __future__ import annotations

import asyncio
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.db import SessionLocal, init_db
from app.integrations import ratelimit
from app.integrations.provider_catalog import PROVIDER_CATALOG, resolve_limits
from app.main import app
from app.models import User, UserSession


@pytest.fixture(autouse=True)
def _clean():
    init_db()
    ratelimit.reset()
    db = SessionLocal()
    db.execute(delete(UserSession)); db.execute(delete(User)); db.commit(); db.close()
    yield


def test_resolve_limits_default_vs_override():
    # Catalog default for a gentle scraper.
    assert resolve_limits("novelupdates", None) == (6.0, 30.0)
    # Operator override wins.
    assert resolve_limits("anilist", {"requests_per_minute": 12, "timeout": 9}) == (12.0, 9.0)
    # Bad/zero values fall back to the default, not "no throttle".
    assert resolve_limits("anilist", {"requests_per_minute": 0}) == (60.0, 15.0)
    # Out-of-range values are clamped.
    rpm, timeout = resolve_limits("anilist", {"requests_per_minute": 99999, "timeout": 0.1})
    assert rpm == 600.0 and timeout == 3.0


def test_throttle_spaces_requests_for_a_key():
    async def run() -> float:
        ratelimit.reset()
        start = time.monotonic()
        # 120/min → 0.5s min gap. First call is immediate; three more space out ~0.5s each.
        for _ in range(3):
            await ratelimit.throttle("k", 120)
        return time.monotonic() - start
    elapsed = asyncio.run(run())
    assert elapsed >= 0.9   # 2 gaps of 0.5s (first call free) ≈ 1.0s, allow scheduling slack


def test_throttle_independent_per_key():
    async def run() -> float:
        ratelimit.reset()
        start = time.monotonic()
        await ratelimit.throttle("a", 60)   # both first-calls → immediate
        await ratelimit.throttle("b", 60)
        return time.monotonic() - start
    assert asyncio.run(run()) < 0.2   # different keys don't wait on each other


def test_catalog_endpoint_lists_every_provider():
    with TestClient(app) as c:
        c.post("/api/auth/setup", json={"username": "admin", "password": "hunter2pw"})
        r = c.get("/api/integrations/catalog")
        assert r.status_code == 200
        rows = r.json()
        assert {p["kind"] for p in rows} == {p["kind"] for p in PROVIDER_CATALOG}
        anilist = next(p for p in rows if p["kind"] == "anilist")
        assert anilist["category"] == "metadata"
        assert anilist["default_rpm"] == 60 and anilist["default_timeout"] == 15
        assert "chapter count" in anilist["provides"]
        # Every entry carries the use / requests / matching copy the boxes render.
        assert all(p["use"] and p["requests"] and p["matching"] for p in rows)
