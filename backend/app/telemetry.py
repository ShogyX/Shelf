"""Outbound-request telemetry.

Counts every external HTTP request the app makes — crawl, metadata APIs, integrations, ingestion,
image/cover fetches, Cloudflare solvers — by destination host × category × OUTCOME × UTC-hour bucket,
for the Settings → Index request dashboard (totals, rates, outcome breakdown, over-time line chart).

Design: increments are CHEAP and in-memory (a lock + dict) so the continuous crawl never pays a DB
write per request; a scheduler tick (``flush``) upserts the accumulated deltas into ``request_stats``.
Most call sites get counted for free by building their httpx client via :func:`instrument` — which
wraps the transport so it records BOTH responses (status → success/blocked/error) AND raised
exceptions (timeout/connection error). Non-httpx egress (headless renders, the zendriver subprocess)
calls :func:`record` directly with an explicit outcome.
"""
from __future__ import annotations

import threading
from collections import defaultdict
from datetime import UTC, datetime

import httpx

# Stable category set surfaced in the dashboard. "other" catches anything unlabelled.
CATEGORIES = ("crawl", "metadata", "integration", "libgen", "image", "export", "solver", "other")
# Request outcomes (what happened), in display order.
OUTCOMES = ("success", "blocked", "timeout", "error")

_lock = threading.Lock()
# (bucket, host, category, outcome) -> count, for deltas not yet flushed to the DB.
_pending: dict[tuple[str, str, str, str], int] = defaultdict(int)


def _bucket(ts: datetime) -> str:
    return ts.astimezone(UTC).strftime("%Y-%m-%dT%H:00")


def _bucket_hours_ago(h: float) -> str:
    return _bucket(datetime.fromtimestamp(datetime.now(UTC).timestamp() - h * 3600, UTC))


def _norm_host(host: str | None) -> str:
    h = (host or "").strip().lower()
    if h.startswith("www."):
        h = h[4:]
    return h or "unknown"


def record(host: str | None, category: str, outcome: str = "success", n: int = 1) -> None:
    """Count ``n`` requests to ``host`` (``category``/``outcome``). Never raises (best-effort)."""
    try:
        key = (_bucket(datetime.now(UTC)), _norm_host(host),
               category if category in CATEGORIES else "other",
               outcome if outcome in OUTCOMES else "error")
        with _lock:
            _pending[key] += n
    except Exception:  # noqa: BLE001
        pass


def outcome_for_status(status: int, headers=None) -> str:
    """Classify an HTTP response: blocked (anti-bot / rate-limit), error (4xx/5xx), else success."""
    try:
        if headers is not None and (headers.get("cf-mitigated") or "").strip():
            return "blocked"
    except Exception:  # noqa: BLE001
        pass
    if status in (403, 429, 451):
        return "blocked"
    if status >= 400:
        return "error"
    return "success"


def _outcome_for_exc(exc: Exception) -> str:
    return "timeout" if isinstance(exc, httpx.TimeoutException) else "error"


class _AsyncTelemetryTransport(httpx.AsyncBaseTransport):
    """Wraps a real transport, recording each request's outcome (response status OR raised error)."""
    def __init__(self, inner: httpx.AsyncBaseTransport, category: str) -> None:
        self._inner, self._category = inner, category

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        try:
            resp = await self._inner.handle_async_request(request)
        except Exception as exc:  # noqa: BLE001 — record then re-raise
            record(host, self._category, _outcome_for_exc(exc))
            raise
        record(host, self._category, outcome_for_status(resp.status_code, resp.headers))
        return resp

    async def aclose(self) -> None:
        await self._inner.aclose()


class _SyncTelemetryTransport(httpx.BaseTransport):
    def __init__(self, inner: httpx.BaseTransport, category: str) -> None:
        self._inner, self._category = inner, category

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        try:
            resp = self._inner.handle_request(request)
        except Exception as exc:  # noqa: BLE001
            record(host, self._category, _outcome_for_exc(exc))
            raise
        record(host, self._category, outcome_for_status(resp.status_code, resp.headers))
        return resp

    def close(self) -> None:
        self._inner.close()


def async_transport(category: str, **transport_kwargs) -> httpx.AsyncBaseTransport:
    return _AsyncTelemetryTransport(httpx.AsyncHTTPTransport(**transport_kwargs), category)


def instrument(category: str, **kwargs) -> httpx.AsyncClient:
    """An ``httpx.AsyncClient`` that auto-counts every request (with outcome) under ``category``."""
    return httpx.AsyncClient(transport=async_transport(category), **kwargs)


def instrument_sync(category: str, **kwargs) -> httpx.Client:
    """A sync ``httpx.Client`` that auto-counts every request (with outcome) under ``category``."""
    return httpx.Client(transport=_SyncTelemetryTransport(httpx.HTTPTransport(), category), **kwargs)


def drain() -> dict[tuple[str, str, str, str], int]:
    with _lock:
        d = dict(_pending)
        _pending.clear()
    return d


def flush(db, *, retain_hours: int = 24 * 30) -> int:
    """Upsert pending in-memory deltas into ``request_stats`` and prune buckets older than
    ``retain_hours``. Returns the number of (bucket,host,category,outcome) rows written. Best-effort."""
    deltas = drain()
    if not deltas:
        return 0
    from sqlalchemy import text
    sql = text(
        "INSERT INTO request_stats (bucket, host, category, outcome, count) "
        "VALUES (:b, :h, :c, :o, :n) "
        "ON CONFLICT(bucket, host, category, outcome) DO UPDATE SET count = count + excluded.count"
    )
    try:
        for (bucket, host, category, outcome), n in deltas.items():
            db.execute(sql, {"b": bucket, "h": host, "c": category, "o": outcome, "n": n})
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
    """Aggregate the last ``hours`` of request_stats for the dashboard: totals, per-category / per-host
    / per-outcome breakdowns, derived rates, and a dense hourly time-series (total + per-outcome) for
    the over-time line chart."""
    from sqlalchemy import text
    since = _bucket_hours_ago(hours)
    rows = db.execute(
        text("SELECT bucket, host, category, outcome, count FROM request_stats WHERE bucket >= :since "
             "ORDER BY bucket"),
        {"since": since},
    ).all()

    total = sum(r.count for r in rows)
    by_cat: dict[str, int] = defaultdict(int)
    by_host: dict[str, int] = defaultdict(int)
    by_outcome: dict[str, int] = defaultdict(int)
    series: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))  # bucket -> outcome -> n
    for r in rows:
        oc = r.outcome if r.outcome in OUTCOMES else "error"
        by_cat[r.category] += r.count
        by_host[r.host] += r.count
        by_outcome[oc] += r.count
        series[r.bucket][oc] += r.count

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
        "by_outcome": [{"outcome": o, "count": by_outcome.get(o, 0)} for o in OUTCOMES],
        "by_host": sorted(
            ({"host": h, "count": n} for h, n in by_host.items()),
            key=lambda x: x["count"], reverse=True)[:30],
        # Dense hourly series for the line chart: total + each outcome per bucket.
        "series": [
            {"bucket": b, "total": sum(series[b].values()),
             "by_outcome": {o: series[b].get(o, 0) for o in OUTCOMES}}
            for b in sorted(series)
        ],
        "outcomes": list(OUTCOMES),
        "categories": cats,
    }
