"""Process-wide outbound request throttle, keyed per integration kind.

Metadata providers and acquisition clients all funnel their HTTP calls through ``throttle(key, rpm)``
before each request, which enforces a minimum spacing of ``60 / rpm`` seconds between consecutive
calls for that key. This keeps a bulk enrichment sweep (or a burst of acquire calls) from tripping a
provider's rate limit / Cloudflare block. Per-key spacing only — concurrency across DIFFERENT kinds
is unaffected, so one slow provider never blocks the others.

Keyed by ``kind`` (not integration id): there is normally one integration per kind, and the cap is a
politeness budget toward the upstream service, which is per-service.
"""
from __future__ import annotations

import asyncio
import time

# kind -> (lock, next-allowed monotonic timestamp). The lock serializes the gap computation so two
# concurrent callers can't both read the same "next allowed" and fire together.
_state: dict[str, tuple[asyncio.Lock, list[float]]] = {}
_guard = asyncio.Lock()


async def _slot(key: str) -> tuple[asyncio.Lock, list[float]]:
    async with _guard:
        if key not in _state:
            _state[key] = (asyncio.Lock(), [0.0])
        return _state[key]


# Reactive cooldown, set after an upstream rate-limit / quota / unavailable response (429/503).
# Where ``throttle`` PROACTIVELY spaces calls, this REACTIVELY stops them: callers consult
# ``cooling_down(key)`` and skip the request entirely while it's hot, so a provider that's already
# said "you're over quota" (e.g. Google Books' daily limit) isn't re-hit every tick. Keyed the same
# as throttle (per kind). A plain dict — get/set are atomic under the GIL.
_cooldown: dict[str, float] = {}  # key -> monotonic expiry


def penalize(key: str, seconds: float) -> None:
    """Hold off ALL further calls for ``key`` for ``seconds`` after an upstream 429/503. Only ever
    EXTENDS an existing cooldown (a later 429 can't shorten an earlier, longer one)."""
    if seconds <= 0:
        return
    until = time.monotonic() + seconds
    if until > _cooldown.get(key, 0.0):
        _cooldown[key] = until


def cooling_down(key: str) -> float:
    """Seconds remaining before ``key`` may be called again (0.0 if not cooling down)."""
    until = _cooldown.get(key)
    if not until:
        return 0.0
    rem = until - time.monotonic()
    if rem <= 0:
        _cooldown.pop(key, None)
        return 0.0
    return rem


async def throttle(key: str, rpm: float) -> None:
    """Block until it's polite to make the next request for ``key`` (≥ 60/rpm since the last)."""
    if rpm <= 0:
        return
    min_gap = 60.0 / rpm
    lock, nxt = await _slot(key)
    async with lock:
        now = time.monotonic()
        wait = nxt[0] - now
        if wait > 0:
            await asyncio.sleep(wait)
            now = time.monotonic()
        nxt[0] = now + min_gap


def reset() -> None:
    """Clear all throttle state (tests)."""
    _state.clear()
    _cooldown.clear()
