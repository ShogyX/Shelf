"""Live-editable crawl speed tuning (Settings → Indexing).

The throughput knobs below were previously env-only (read once at startup via the cached
Settings), so the operator couldn't speed the crawler up without a restart. Here they live in
the ``app_settings`` key/value table and are read fresh each scheduler tick, so a change takes
effect on running AND future jobs. Changing the tick cadence or parallelism also re-applies the
side effects (reschedule the scheduler jobs, resize the global fetch semaphore).

Defaults are the "Moderate" profile: noticeably faster than the old 15s / 1-chapter / 2-parallel
baseline, while per-source politeness intervals (unchanged) still prevent hammering any one site.
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

log = logging.getLogger("shelf.crawl_tuning")

_KEY = "crawl_tuning"

# key -> (default, min, max)
_SPEC = {
    "tick_seconds": (10, 2, 600),        # how often a crawl/index cycle runs
    "chapters_per_tick": (3, 1, 50),     # chapters one backfill job fetches per cycle
    "parallel_fetches": (4, 1, 32),      # per-cycle work/page cap + global fetch concurrency
}


def defaults() -> dict[str, int]:
    return {k: v[0] for k, v in _SPEC.items()}


def _clamp(key: str, value) -> int:
    default, lo, hi = _SPEC[key]
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return default


def get_tuning(db: Session) -> dict[str, int]:
    """Current tuning (operator overrides merged over defaults), every value clamped."""
    from ..models import AppSetting

    out = defaults()
    row = db.get(AppSetting, _KEY)
    if row and isinstance(row.value, dict):
        for k in _SPEC:
            if k in row.value:
                out[k] = _clamp(k, row.value[k])
    return out


def set_tuning(db: Session, updates: dict[str, int]) -> dict[str, int]:
    """Persist overrides, then re-apply side effects (semaphore size + scheduler cadence)."""
    from ..models import AppSetting

    current = get_tuning(db)
    for k, v in (updates or {}).items():
        if k in _SPEC and v is not None:
            current[k] = _clamp(k, v)

    row = db.get(AppSetting, _KEY)
    if row is None:
        row = AppSetting(key=_KEY, value=dict(current))
        db.add(row)
    else:
        row.value = dict(current)
    db.commit()

    apply_runtime(current)
    return current


def apply_runtime(tuning: dict[str, int]) -> None:
    """Push tuning into the running process: resize the global fetch semaphore and reschedule
    the scheduler's crawl/index ticks. Safe to call when the scheduler isn't running yet."""
    try:
        from .engine import get_fetcher
        get_fetcher().set_concurrency(tuning["parallel_fetches"])
    except Exception:  # noqa: BLE001 — never let a settings change crash the request
        log.exception("failed to resize fetch concurrency")
    try:
        from .scheduler import reschedule_crawl_ticks
        reschedule_crawl_ticks(tuning["tick_seconds"])
    except Exception:  # noqa: BLE001
        log.exception("failed to reschedule crawl ticks")
