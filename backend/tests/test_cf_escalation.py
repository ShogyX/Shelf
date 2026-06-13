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
        f._host_solver[f._bucket_key(sk, k.get("rate_key"))] = "zendriver"   # mark sticky (as the real one does)
        return good                          # zendriver passes Turnstile
    f._zendriver_render = _zen

    out = await f.get_html("web_index:x.com", "http://x.com/p")
    assert out is good
    assert calls == {"get": 1, "flare": 1, "render": 1, "zen": 1}   # tried each tier in order

    # Sticky: the next fetch goes STRAIGHT to zendriver — no plain GET / FlareSolverr / render.
    out2 = await f.get_html("web_index:x.com", "http://x.com/p")
    assert out2 is good
    assert calls == {"get": 1, "flare": 1, "render": 1, "zen": 2}


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
