"""Reactive rate-limit cooldown: a 429/503 stops further upstream calls until it lapses."""
import httpx

from app.integrations import ratelimit
from app.integrations.metadata import _cooldown_after, _RL_DEFAULT_COOLDOWN_S, _RL_MAX_COOLDOWN_S


def _resp(status, headers=None):
    return httpx.Response(status, headers=headers or {})


def test_penalize_and_cooling_down():
    ratelimit.reset()
    assert ratelimit.cooling_down("googlebooks") == 0.0
    ratelimit.penalize("googlebooks", 60)
    assert 0 < ratelimit.cooling_down("googlebooks") <= 60
    # a SHORTER later penalty never shortens an active cooldown
    ratelimit.penalize("googlebooks", 5)
    assert ratelimit.cooling_down("googlebooks") > 30
    ratelimit.reset()
    assert ratelimit.cooling_down("googlebooks") == 0.0


def test_cooldown_after_retry_after_seconds():
    assert _cooldown_after(_resp(429, {"Retry-After": "120"})) == 120.0


def test_cooldown_after_default_when_no_header():
    # Google Books daily-quota 429 carries no Retry-After → default cooldown.
    assert _cooldown_after(_resp(429)) == _RL_DEFAULT_COOLDOWN_S


def test_cooldown_after_capped():
    assert _cooldown_after(_resp(429, {"Retry-After": "99999"})) == _RL_MAX_COOLDOWN_S
