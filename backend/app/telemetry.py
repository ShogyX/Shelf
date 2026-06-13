"""Outbound-request telemetry.

Counts every external HTTP request the app makes — crawl, metadata APIs, integrations, ingestion,
image/cover fetches, Cloudflare solvers — by destination host × category × UTC-hour bucket, for the
Settings → Index request dashboard (totals, rates, trends).

Design: increments are CHEAP and in-memory (a lock + dict) so the continuous crawl never pays a DB
write per request; a scheduler tick (``flush``) upserts the accumulated deltas into ``request_stats``.
Most call sites get counted for free by building their httpx client via :func:`instrument` (an event
hook records every response); the few non-httpx egress points (headless renders, the zendriver
subprocess) call :func:`record` directly.
"""
from __future__ import annotations

import threading
from collections import defaultdict
from datetime import UTC, datetime

import httpx

# Stable category set surfaced in the dashboard. "other" catches anything unlabelled.
CATEGORIES = ("crawl", "metadata", "integration", "libgen", "image", "export", "solver", "other")

_lock = threading.Lock()
# (bucket, host, category) -> count, for deltas not yet flushed to the DB.
_pending: dict[tuple[str, str, str], int] = defaultdict(int)


def _bucket(ts: datetime) -> str:
    return ts.astimezone(UTC).strftime("%Y-%m-%dT%H:00")


def _bucket_hours_ago(h: float) -> str:
    return _bucket(datetime.fromtimestamp(datetime.now(UTC).timestamp() - h * 3600, UTC))


def _norm_host(host: str | None) -> str:
    h = (host or "").strip().lower()
    if h.startswith("www."):
        h = h[4:]
    return h or "unknown"


def record(host: str | None, category: str, n: int = 1) -> None:
    """Count ``n`` requests to ``host`` under ``category``. Never raises (telemetry is best-effort)."""
    try:
        key = (_bucket(datetime.now(UTC)), _norm_host(host), category if category in CATEGORIES else "other")
        with _lock:
            _pending[key] += n
    except Exception:  # noqa: BLE001
        pass


def _response_hook(category: str):
    async def hook(resp: httpx.Response) -> None:
        try:
            record(resp.request.url.host, category)
        except Exception:  # noqa: BLE001
            pass
    return hook


def response_hook(category: str):
    """An httpx 'response' event-hook that counts each request under ``category`` (to append onto an
    existing client's event_hooks)."""
    return _response_hook(category)


def instrument(category: str, **kwargs) -> httpx.AsyncClient:
    """Build an ``httpx.AsyncClient`` that auto-counts every request it makes under ``category``."""
    hooks = dict(kwargs.pop("event_hooks", {}) or {})
    hooks["response"] = list(hooks.get("response", [])) + [_response_hook(category)]
    return httpx.AsyncClient(event_hooks=hooks, **kwargs)


def _sync_response_hook(category: str):
    def hook(resp: httpx.Response) -> None:
        try:
            record(resp.request.url.host, category)
        except Exception:  # noqa: BLE001
            pass
    return hook


def instrument_sync(category: str, **kwargs) -> httpx.Client:
    """Build a sync ``httpx.Client`` that auto-counts every request under ``category``."""
    hooks = dict(kwargs.pop("event_hooks", {}) or {})
    hooks["response"] = list(hooks.get("response", [])) + [_sync_response_hook(category)]
    return httpx.Client(event_hooks=hooks, **kwargs)


def drain() -> dict[tuple[str, str, str], int]:
    with _lock:
        d = dict(_pending)
        _pending.clear()
    return d


def flush(db, *, retain_hours: int = 24 * 30) -> int:
    """Upsert pending in-memory deltas into ``request_stats`` and prune buckets older than
    ``retain_hours``. Returns the number of (bucket,host,category) rows written. Best-effort."""
    deltas = drain()
    if not deltas:
        return 0
    from sqlalchemy import text
    sql = text(
        "INSERT INTO request_stats (bucket, host, category, count) VALUES (:b, :h, :c, :n) "
        "ON CONFLICT(bucket, host, category) DO UPDATE SET count = count + excluded.count"
    )
    try:
        for (bucket, host, category), n in deltas.items():
            db.execute(sql, {"b": bucket, "h": host, "c": category, "n": n})
        # Prune old buckets so the table stays small (string buckets sort lexically by time).
        db.execute(text("DELETE FROM request_stats WHERE bucket < :cutoff"),
                   {"cutoff": _bucket_hours_ago(retain_hours)})
        db.commit()
    except Exception:  # noqa: BLE001 — on failure, re-stash the deltas so they aren't lost
        db.rollback()
        with _lock:
            for k, n in deltas.items():
                _pending[k] += n
        return 0
    return len(deltas)


def summary(db, *, hours: int = 48) -> dict:
    """Aggregate the last ``hours`` of request_stats for the dashboard: totals + per-category +
    per-host breakdowns, derived rates (per s/min/hour/day), and an hourly time-series for trends."""
    from sqlalchemy import text
    since = _bucket_hours_ago(hours)
    rows = db.execute(
        text("SELECT bucket, host, category, count FROM request_stats WHERE bucket >= :since "
             "ORDER BY bucket"),
        {"since": since},
    ).all()

    total = sum(r.count for r in rows)
    by_cat: dict[str, int] = defaultdict(int)
    by_host: dict[str, int] = defaultdict(int)
    series: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))  # bucket -> cat -> count
    for r in rows:
        by_cat[r.category] += r.count
        by_host[r.host] += r.count
        series[r.bucket][r.category] += r.count

    # Rates derived from the last 24h average (smoother than a single bucket).
    last_hour = _bucket(datetime.now(UTC))
    last_hour_total = sum(r.count for r in rows if r.bucket == last_hour)
    cutoff_24h = _bucket_hours_ago(24)
    last_24h = sum(r.count for r in rows if r.bucket >= cutoff_24h)
    per_hour = last_24h / 24.0
    rates = {
        "per_second": round(per_hour / 3600.0, 4),
        "per_minute": round(per_hour / 60.0, 2),
        "per_hour": round(per_hour, 1),
        "per_day": last_24h,
        "current_hour": last_hour_total,
    }
    cats = sorted(by_cat)
    return {
        "window_hours": hours,
        "total": total,
        "rates": rates,
        "by_category": [{"category": c, "count": by_cat[c]} for c in cats],
        "by_host": sorted(
            ({"host": h, "count": n} for h, n in by_host.items()),
            key=lambda x: x["count"], reverse=True)[:30],
        # Dense hourly series (gaps filled with 0) so the chart x-axis is continuous.
        "series": [
            {"bucket": b, "total": sum(series[b].values()),
             "by_category": {c: series[b].get(c, 0) for c in cats}}
            for b in sorted(series)
        ],
        "categories": cats,
    }
