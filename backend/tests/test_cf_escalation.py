"""The PoliteFetcher auto-escalation ladder: plain HTTP → FlareSolverr → in-app render → zendriver,
tried in cost order on a Cloudflare CHALLENGE, with the winning tier remembered per host so a site
that newly adds (or escalates) CF is handled automatically."""
from __future__ import annotations

from app.ingestion.browser import RenderedPage
from app.ingestion.fetcher import PoliteFetcher, RateLimited

_INTERSTITIAL = ("<html><head><title>Just a moment...</title></head><body>"
                 "<div class='cf-turnstile'></div> checking your browser</body></html>")
_SOLVED = "<html><body>" + ("<img src=x> word " * 400) + \
          "<script src='/cdn-cgi/challenge-platform/x.js'></script></body></html>"


def _page(html: str, status: int = 200) -> RenderedPage:
    return RenderedPage(status=status, text=html, url="http://x/", body_text=html)


def test_result_is_challenge_distinguishes_solved_from_interstitial():
    f = PoliteFetcher("t", "t@t")
    # A short, image-less interstitial → still a challenge.
    assert f._result_is_challenge(_page(_INTERSTITIAL)) is True
    # A long, image-rich SOLVED page that merely still embeds the /challenge-platform/ script → NOT
    # a challenge (the false-positive that would otherwise loop the escalation forever).
    assert f._result_is_challenge(_page(_SOLVED)) is False

    # A hard 403 carrying a Cloudflare header → challenge.
    class _Resp:
        status_code = 403
        headers = {"cf-mitigated": "challenge"}
        text = "just a moment..."
    assert f._result_is_challenge(_Resp()) is True


async def test_escalation_falls_through_to_zendriver_then_sticks(monkeypatch):
    f = PoliteFetcher("t", "t@t")
    calls = {"get": 0, "flare": 0, "render": 0, "zen": 0}

    async def _allow(*a, **k):
        return True
    f.allowed = _allow                       # skip robots fetch
    f._browser_usable = lambda: True

    async def _get(sk, url, **k):
        calls["get"] += 1
        raise RateLimited("blocked", challenge=True)   # plain HTTP is challenged
    f.get = _get

    async def _flare(sk, url, **k):
        calls["flare"] += 1
        return None                          # FlareSolverr can't solve it
    f._solver_retry = _flare

    async def _render(sk, url, **k):
        calls["render"] += 1
        return _page(_INTERSTITIAL)          # in-app render still challenged
    f._render = _render

    good = _page(_SOLVED)

    async def _zen(sk, url, **k):
        calls["zen"] += 1
        f._remember_solver(f._bucket_key(sk, k.get("rate_key")), "zendriver")   # mark sticky (as the real one does)
        return good                          # zendriver passes Turnstile
    f._zendriver_render = _zen

    out = await f.get_html("web_index:x.com", "http://x.com/p")
    assert out is good
    assert calls == {"get": 1, "flare": 1, "render": 1, "zen": 1}   # tried each tier in order

    # Sticky: the next fetch goes STRAIGHT to zendriver — no plain GET / FlareSolverr / render.
    out2 = await f.get_html("web_index:x.com", "http://x.com/p")
    assert out2 is good
    assert calls == {"get": 1, "flare": 1, "render": 1, "zen": 2}


async def test_sticky_solver_expires_and_re_evaluates(monkeypatch):
    """A one-off challenge must NOT pin a host to zendriver forever: once the sticky window lapses,
    the ladder re-evaluates from plain HTTP (self-healing a transient CF blip)."""
    f = PoliteFetcher("t", "t@t")
    calls = {"get": 0, "zen": 0}

    async def _allow(*a, **k):
        return True
    f.allowed = _allow
    f._browser_usable = lambda: True
    f._solver_retry = lambda *a, **k: _awaitable(None)
    f._render = lambda *a, **k: _awaitable(_page(_INTERSTITIAL))
    good = _page(_SOLVED)

    async def _get(sk, url, **k):
        calls["get"] += 1
        raise RateLimited("blocked", challenge=True)
    f.get = _get

    async def _zen(sk, url, **k):
        calls["zen"] += 1
        f._remember_solver(f._bucket_key(sk, k.get("rate_key")), "zendriver")
        return good
    f._zendriver_render = _zen

    await f.get_html("web_index:x.com", "http://x.com/p")
    assert calls == {"get": 1, "zen": 1}
    # Fresh sticky → straight to zendriver, no plain GET.
    await f.get_html("web_index:x.com", "http://x.com/p")
    assert calls == {"get": 1, "zen": 2}

    # Age BOTH sticky signals past the TTL (they're set together in the real flow): the solver tier
    # AND the budget render-elevation. The next fetch must fall back through plain HTTP again.
    bucket = f._bucket_key("web_index:x.com", None)
    f._host_solver_at[bucket] -= f._STICKY_TTL_S + 1
    f._budgets[bucket]._render_elevated_until = 0.0
    await f.get_html("web_index:x.com", "http://x.com/p")
    assert calls == {"get": 2, "zen": 3}   # re-tried plain HTTP, then re-escalated


def test_budget_render_elevation_expires():
    from app.ingestion.fetcher import SourceBudget
    b = SourceBudget(min_request_interval_s=0.1, max_daily_requests=100)
    clock = [1000.0]
    now = lambda: clock[0]
    assert b.wants_render(now) is False          # neither configured nor elevated
    b.elevate_render(300.0, now)
    assert b.wants_render(now) is True           # elevated
    clock[0] += 299
    assert b.wants_render(now) is True           # still inside the window
    clock[0] += 2
    assert b.wants_render(now) is False          # window lapsed → de-escalated
    # A configured render_js source always wants render, regardless of elevation.
    b.render_js = True
    assert b.wants_render(now) is True


def test_prune_idle_evicts_idle_derived_buckets_only():
    f = PoliteFetcher("t", "t@t")
    clock = [1000.0]
    now = lambda: clock[0]
    # Two derived (rate-keyed) buckets + one source-own budget.
    f._rate_budget("web_index", "web_index:old.com")
    f._host_sem("web_index:old.com")
    f._remember_solver("web_index:old.com", "zendriver", now)
    f._bucket_seen["web_index:old.com"] = now()
    f._rate_budget("web_index", "web_index:fresh.com")
    f._budget("web_index")   # source-own budget (no ':') — must never be pruned
    # Advance so old.com is idle beyond the window but touch fresh.com right before the sweep.
    clock[0] += 7200
    f._bucket_seen["web_index:fresh.com"] = now()
    pruned = f.prune_idle(max_idle_s=3600.0, now_fn=now)
    assert pruned == 1
    assert "web_index:old.com" not in f._budgets
    assert "web_index:old.com" not in f._host_semaphores
    assert "web_index:old.com" not in f._host_solver
    assert "web_index:fresh.com" in f._budgets      # recently touched → kept
    assert "web_index" in f._budgets                # source-own → never evicted


async def test_prune_idle_skips_in_use_semaphore():
    f = PoliteFetcher("t", "t@t")
    clock = [1000.0]
    now = lambda: clock[0]
    key = "web_index:busy.com"
    f._rate_budget("web_index", key)
    sem = f._host_sem(key)
    f._bucket_seen[key] = now()
    clock[0] += 7200
    await sem.acquire()                       # simulate an in-flight fetch holding the slot
    try:
        assert f.prune_idle(max_idle_s=3600.0, now_fn=now) == 0   # busy → not evicted
        assert key in f._host_semaphores
    finally:
        sem.release()
    assert f.prune_idle(max_idle_s=3600.0, now_fn=now) == 1        # released → evicted


def _awaitable(value):
    async def _c():
        return value
    return _c()


async def test_no_escalation_when_not_a_challenge(monkeypatch):
    """A plain non-challenge failure (e.g. a real 404/ban) must NOT spin up the solver tiers."""
    f = PoliteFetcher("t", "t@t")
    calls = {"zen": 0}

    async def _allow(*a, **k):
        return True
    f.allowed = _allow

    async def _get(sk, url, **k):
        raise RateLimited("plain ban", challenge=False)
    f.get = _get

    async def _zen(sk, url, **k):
        calls["zen"] += 1
        return _page(_SOLVED)
    f._zendriver_render = _zen

    try:
        await f.get_html("web_index:x.com", "http://x.com/p")
        assert False, "should have re-raised"
    except RateLimited:
        pass
    assert calls["zen"] == 0

async def test_solver_retry_preserves_clearance_cookie_after_ua_change(monkeypatch):
    """REGRESSION: set_identity (UA change) rebuilds the HTTP client, so the FlareSolverr clearance
    cookies must be injected AFTER the UA is adopted — otherwise the plain-HTTP replay carries no
    cf_clearance and just re-challenges, silently defeating the whole solver tier."""
    from app.ingestion import flaresolverr

    f = PoliteFetcher("OldUA/1.0", "t@t")

    class _Clearance:
        cookies = {"cf_clearance": "TOKEN123"}
        user_agent = "Mozilla/5.0 SolverUA/9"

    monkeypatch.setattr(flaresolverr, "configured", lambda: True)
    async def _ensure(url):
        return _Clearance()
    monkeypatch.setattr(flaresolverr, "ensure_clearance", _ensure)

    captured = {}
    async def _get(sk, url, **k):
        client = await f._get_client()              # the live client the replay actually uses
        captured["ua"] = f.user_agent
        captured["cookie"] = client.cookies.get("cf_clearance")
        return _page(_SOLVED)
    f.get = _get

    out = await f._solver_retry("web_index:x.com", "http://x.com/p", headers=None, rate_key=None)
    assert out is not None
    assert captured["ua"] == "Mozilla/5.0 SolverUA/9"   # solver UA adopted
    assert captured["cookie"] == "TOKEN123"             # cookie survived the client rebuild
    await f.aclose()
