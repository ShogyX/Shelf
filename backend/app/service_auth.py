"""Service-token authentication for the ``/api/admin/*`` provisioning surface (GATE-1).

A bearer credential for MACHINES. Shelf's admin user management is session-cookie authenticated, so
an external provisioner (which creates an account when someone is granted access, and deactivates it
when the grant ends) cannot drive it. This is that credential, and it is deliberately narrow:

  * Only ``/api/admin/*`` accepts it, and those routes accept NOTHING else — there is no session
    fallback there, and no other route accepts a service token. A stolen cookie is therefore not a
    provisioning credential, and a leaked service token is not a login.
  * ``SHELF_SERVICE_TOKENS`` holds SHA-256 hashes, never the tokens themselves, and each candidate is
    compared with ``hmac.compare_digest`` — a byte-at-a-time ``==`` leaks the matching prefix.
  * Unset = DISABLED. Every request 401s; the surface is never open by omission.
  * Its OWN rate-limit bucket, not app.auth's login throttle: the provisioner reads before every
    create, and charging that against login attempts would lock humans out (and vice versa).

NOTE: in-process rate state, exactly like the login throttle — correct for the supported
single-worker deployment; see the note in app/auth.py before ever enabling --workers > 1.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import threading
import time

from fastapi import HTTPException, Request

from .auth import client_ip
from .config import get_settings

log = logging.getLogger("shelf.service_auth")

_WINDOW = 60.0        # sliding window for both budgets, seconds
_FAIL_LIMIT = 10      # rejected tokens per window per client IP (token guessing)
_MAX_KEYS = 10_000    # memory-DoS guard — same shape as app/auth.py's _fail_log cap

_lock = threading.Lock()
_hits: dict[str, list[float]] = {}
_last_sweep = 0.0
_warned_disabled = False


def token_hash(token: str) -> str:
    """The value that belongs in SHELF_SERVICE_TOKENS. The token itself is never stored by Shelf."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def configured_hashes() -> list[str]:
    """The configured SHA-256 hashes, normalised. Empty = the surface is disabled."""
    return [h.strip().lower() for h in (get_settings().service_tokens or []) if h and h.strip()]


def verify_service_token(presented: str | None) -> str | None:
    """A short, non-reversible id for the matching token, or None.

    Constant-time throughout: the PRESENTED value is hashed first (so its length never reaches a
    comparison), every configured hash is checked with hmac.compare_digest, and the loop does not
    break early — neither the match nor its position is timeable."""
    if not presented:
        return None
    digest = token_hash(presented)
    matched = ""
    for want in configured_hashes():
        if hmac.compare_digest(digest, want):
            matched = digest
    return matched[:12] or None


# ------------------------------------------------------------------ rate limiting (own bucket)
def _sweep(now: float) -> None:
    """Drop EVERY expired key, not just re-queried ones. Called under _lock."""
    global _last_sweep
    if now - _last_sweep < _WINDOW:
        return
    _last_sweep = now
    for key in list(_hits):
        arr = [t for t in _hits[key] if now - t < _WINDOW]
        if arr:
            _hits[key] = arr
        else:
            del _hits[key]


def retry_after(key: str, limit: int) -> int:
    """Seconds until ``key`` is under ``limit`` again, else 0. Peeks — does not charge a hit."""
    now = time.time()
    with _lock:
        _sweep(now)
        arr = [t for t in _hits.get(key, []) if now - t < _WINDOW]
        if arr:
            _hits[key] = arr
        else:
            _hits.pop(key, None)
        if len(arr) >= limit:
            return int(_WINDOW - (now - arr[0])) + 1
    return 0


def record(key: str) -> None:
    now = time.time()
    with _lock:
        _sweep(now)
        if key not in _hits and len(_hits) >= _MAX_KEYS:
            # At the cap, forget the oldest-touched key rather than grow unbounded (as app/auth.py).
            del _hits[min(_hits, key=lambda k: _hits[k][-1])]
        _hits.setdefault(key, []).append(now)


def _too_many(key: str, limit: int) -> None:
    wait = retry_after(key, limit)
    if wait > 0:
        raise HTTPException(
            429, f"Too many requests — try again in {wait}s.",
            headers={"Retry-After": str(wait)},
        )


def _warn_disabled() -> None:
    """Say WHY once, in the operator's log. The 401 on the wire deliberately says nothing about how
    the instance is configured."""
    global _warned_disabled
    if not _warned_disabled:
        _warned_disabled = True
        log.warning(
            "/api/admin/* refused: SHELF_SERVICE_TOKENS is not set, so the service-token admin API "
            "is disabled. Set it to the sha256 hash of each allowed token to enable it."
        )


# ------------------------------------------------------------------------------- dependency
def require_service_token(request: Request) -> str:
    """Router-level gate for /api/admin/*: a valid ``Authorization: Bearer`` service token, or 401.

    Runs BEFORE any session lookup — in fact instead of one: those routes carry no session dependency
    at all, so a cookie (or a session token presented as a bearer) never authenticates here."""
    ip = client_ip(request)
    limit = max(1, get_settings().service_rate_limit)
    _too_many(f"svc:{ip}", limit)
    record(f"svc:{ip}")
    _too_many(f"svc-fail:{ip}", _FAIL_LIMIT)      # tighter budget: token guessing, not normal traffic

    auth = request.headers.get("authorization") or ""
    presented = auth[7:].strip() if auth[:7].lower() == "bearer " else ""
    if not configured_hashes():
        _warn_disabled()
        token_id = None                            # unset = disabled, never open
    else:
        token_id = verify_service_token(presented)
    if token_id is None:
        record(f"svc-fail:{ip}")
        raise HTTPException(401, "Invalid service token")
    return token_id
