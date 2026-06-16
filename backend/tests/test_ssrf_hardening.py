"""Batch-1 SSRF hardening (review 2026-06-15): notify scheme/host guard, metadata per-hop
redirect revalidation, imagecache IP-pinning. Asserts the CORRECTED behaviour."""
from __future__ import annotations

import pytest

from app import notify as N


# --------------------------------------------------------------------- notify target guard (F13/F28)
def test_notify_rejects_generic_http_schemes():
    # json:// / xml:// / form:// are arbitrary-host HTTP request primitives → SSRF. Always refused.
    assert N._target_allowed("json://attacker.example/path") is False
    assert N._target_allowed("xml://10.0.0.1/x") is False
    assert N._target_allowed("form://169.254.169.254/latest") is False
    assert N._target_allowed("file:///etc/passwd") is False


def test_notify_rejects_self_hosted_internal_host(monkeypatch):
    # ntfy/matrix/mqtt carry a user-supplied host → must resolve public.
    monkeypatch.setattr("app.ingestion.netguard.is_public_url", lambda u: False)
    assert N._target_allowed("ntfy://127.0.0.1/topic") is False
    assert N._target_allowed("matrix://user:pw@internal.lan/room") is False


def test_notify_allows_public_self_hosted_and_fixed_vendor(monkeypatch):
    monkeypatch.setattr("app.ingestion.netguard.is_public_url", lambda u: True)
    assert N._target_allowed("ntfy://ntfy.sh/mytopic") is True
    # Fixed-vendor schemes connect to the provider's own host → always allowed (no host check).
    assert N._target_allowed("tgram://bottoken/12345") is True
    assert N._target_allowed("discord://111/abc") is True
    assert N._target_allowed("pover://user@token") is True


def test_notify_blank_url_is_noop():
    assert N.notify("", "t", "b") is False
    assert N.notify(None, "t", "b") is False


def test_notify_denied_url_never_dispatches(monkeypatch):
    # A denied target must short-circuit before apprise is ever invoked.
    called = {"n": 0}

    def _boom(*a, **k):  # pragma: no cover - must not run
        called["n"] += 1
        raise AssertionError("apprise must not be called for a denied target")

    import sys
    import types
    fake = types.ModuleType("apprise")
    fake.Apprise = _boom
    monkeypatch.setitem(sys.modules, "apprise", fake)
    assert N.notify("json://10.0.0.5/x", "t", "b") is False
    assert called["n"] == 0


# --------------------------------------------------------------------- metadata per-hop SSRF (F12/F29)
@pytest.mark.asyncio
async def test_metadata_redirect_to_internal_is_blocked(monkeypatch):
    """A provider that 302s to an internal address must be blocked on the hop, not followed."""
    from app.integrations import metadata as M
    from app.integrations.base import IntegrationError
    from app.ingestion import netguard

    # First hop public, redirect target private — real assert_public_url decides via _ip_is_public.
    def fake_resolve(url):
        from urllib.parse import urlparse
        host = urlparse(url).hostname
        if host == "provider.example":
            return ["93.184.216.34"]  # public
        raise netguard.BlockedAddress(f"blocked internal {host}")

    monkeypatch.setattr(M, "assert_public_url", fake_resolve, raising=False)
    # Patch the symbol imported inside _request (it imports from netguard at call time).
    monkeypatch.setattr(netguard, "assert_public_url", fake_resolve)

    class _RedirResp:
        is_redirect = True
        headers = {"location": "http://169.254.169.254/latest/meta-data/"}

    class _C:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def request(self, *a, **k): return _RedirResp()

    monkeypatch.setattr(M.telemetry, "instrument", lambda *a, **k: _C())
    from app.integrations import ratelimit as RL
    monkeypatch.setattr(RL, "throttle", _noop_async)

    prov = M.MetadataProvider(base_url="http://provider.example")
    with pytest.raises(IntegrationError):
        await prov._request("GET", "http://provider.example/x")


async def _noop_async(*a, **k):
    return None
