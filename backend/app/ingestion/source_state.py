"""Per-(work, source) search-state machine (Wave B).

The title-level :mod:`ledger` gate is coarse — once a title is "unavailable" it gates EVERY route.
This module tracks the search state of each DURABLE download source (torrent / pipeline / libgen)
for a title independently, via a :class:`WorkSourceSearch` child row of the title's
:class:`ContentRequest`. That lets a transient Prowlarr outage (which blocks the usenet search) NOT
permanently lock the title out across all routes, and lets the source-retry tick re-search only the
one source whose backoff is due.

State per source row:
  pending     never searched, or reset by an admin recheck → eligible to lease + search
  searching   leased (a token + leased_at stamped); a searcher is in flight
  no_match    searched, found nothing → TERMINAL (skipped on every later acquire, incl. force)
  exhausted   candidates were tried, all broke/unverified → TERMINAL
  unavailable the search BACKEND was unreachable (Prowlarr 503/quota) → retried at next_retry_at
  matched     a job/hook was started for this source
  skipped     another source imported the title → this source's queued search is moot (R20)

Lease: a CAS UPDATE (mirrors :class:`CrawlJob`) claims a leasable row (status pending|unavailable,
lease free or stale) for ONE searcher — killing the tick-vs-live double-search race.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ..models import ContentRequest, Integration, SourceAttempt, WorkSourceSearch

# The 3 durable download sources that get a per-source search row (web_index/readarr/kapowarr are
# row-existence checks, not durable searches).
DURABLE_SOURCES = ("torrent", "pipeline", "libgen")
# Terminal statuses: a real no-match or an exhausted candidate list — never re-searched on a normal
# acquire (R22), even under force (an admin recheck RESETs these to pending FIRST, then re-acquires).
_TERMINAL = frozenset({"no_match", "exhausted"})
# Statuses a lease may claim: a fresh/reset source, or one whose transient backoff is being retried.
_LEASABLE = frozenset({"pending", "unavailable"})
# A lease whose leased_at is older than this is stale (its searcher crashed) → re-claimable.
_LEASE_STALE = timedelta(minutes=30)
# Per-source daily availability window (for the opt-in max_daily_requests cap).
_DAY = timedelta(hours=24)
# Which Integration.kind carries the opt-in max_daily_requests cap for each durable source.
_SOURCE_INTEGRATION = {"torrent": "prowlarr", "pipeline": "sabnzbd", "libgen": "libgen"}


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def ensure_rows(db: Session, req: ContentRequest, sources) -> None:
    """Idempotently create a ``pending`` row for each of ``sources`` missing on ``req``. Called at the
    top of acquire so a fresh title has all its durable-source rows in ``pending`` (the lease then
    never skips a source). No-op for rows that already exist (any status)."""
    if req is None:
        return
    have = set(db.scalars(select(WorkSourceSearch.source).where(
        WorkSourceSearch.content_request_id == req.id)).all())
    for s in sources:
        if s in DURABLE_SOURCES and s not in have:
            db.add(WorkSourceSearch(content_request_id=req.id, source=s, status="pending"))
    db.commit()


def terminal_sources(db: Session, req: ContentRequest) -> set[str]:
    """The sources whose row is in a TERMINAL state (no_match/exhausted) — the acquire skip-set
    (R22), honored even under ``force``."""
    if req is None:
        return set()
    return set(db.scalars(select(WorkSourceSearch.source).where(
        WorkSourceSearch.content_request_id == req.id,
        WorkSourceSearch.status.in_(tuple(_TERMINAL)))).all())


def lease(db: Session, req: ContentRequest, source: str) -> str | None:
    """CAS-claim the (req, source) row for one searcher: stamp a fresh token + leased_at iff its
    status is leasable (pending|unavailable) AND its lease is free or stale. Returns the token on a
    win, None if another searcher holds a fresh lease or the row is in a non-leasable state. Kills the
    retry-tick-vs-live-acquire double-search race."""
    if req is None:
        return None
    token = uuid.uuid4().hex
    now = _utcnow()
    stale_before = now - _LEASE_STALE
    res = db.execute(
        update(WorkSourceSearch)
        .where(
            WorkSourceSearch.content_request_id == req.id,
            WorkSourceSearch.source == source,
            WorkSourceSearch.status.in_(tuple(_LEASABLE)),
            (WorkSourceSearch.lease_token.is_(None)) | (WorkSourceSearch.leased_at < stale_before),
        )
        .values(status="searching", lease_token=token, leased_at=now)
    )
    db.commit()
    return token if res.rowcount else None


def record(db: Session, req: ContentRequest, source: str, status: str, *,
           reason: str | None = None, http_status: int | None = None,
           retry_at: datetime | None = None) -> None:
    """Transition the (req, source) row to ``status`` and RELEASE its lease. Bumps attempts + stamps
    last_attempt_at. ``next_retry_at`` is set for an ``unavailable`` row (when the source-retry tick
    re-searches), cleared otherwise. No-op when ``req`` is None."""
    if req is None:
        return
    now = _utcnow()
    db.execute(
        update(WorkSourceSearch)
        .where(WorkSourceSearch.content_request_id == req.id, WorkSourceSearch.source == source)
        .values(
            status=status, reason=reason, last_http_status=http_status,
            last_attempt_at=now, next_retry_at=(retry_at if status == "unavailable" else None),
            lease_token=None, leased_at=None,
            attempts=WorkSourceSearch.attempts + 1,
        )
    )
    db.commit()


def drop_upstream_unavailable(db: Session, req: ContentRequest, keep_source: str | None = None) -> None:
    """R20: a REAL import resolved the title → the OTHER sources' queued ``unavailable`` searches are
    moot. Mark them ``skipped`` (never re-search them) — but ONLY ``unavailable`` rows (a terminal
    no_match/exhausted stays as-is; a matched/skipped row is untouched). ``keep_source`` (the source
    that imported) is left alone."""
    if req is None:
        return
    db.execute(
        update(WorkSourceSearch)
        .where(
            WorkSourceSearch.content_request_id == req.id,
            WorkSourceSearch.status == "unavailable",
            WorkSourceSearch.source != (keep_source or ""),
        )
        .values(status="skipped", lease_token=None, leased_at=None)
    )
    db.commit()


def due_unavailable(db: Session, *, limit: int) -> list[WorkSourceSearch]:
    """The ``unavailable`` source rows whose ``next_retry_at`` is now due and whose lease is free or
    stale, parent NOT resolved — the source-retry tick's work queue (oldest-due first, capped)."""
    now = _utcnow()
    stale_before = now - _LEASE_STALE
    return list(db.scalars(
        select(WorkSourceSearch)
        .join(ContentRequest, ContentRequest.id == WorkSourceSearch.content_request_id)
        .where(
            WorkSourceSearch.status == "unavailable",
            WorkSourceSearch.next_retry_at.is_not(None),
            WorkSourceSearch.next_retry_at <= now,
            (WorkSourceSearch.lease_token.is_(None)) | (WorkSourceSearch.leased_at < stale_before),
            ContentRequest.status != "resolved",
        )
        .order_by(WorkSourceSearch.next_retry_at)
        .limit(limit)
    ).all())


def reap_stale_leases(db: Session) -> int:
    """Return any ``searching`` row whose lease has gone stale (its searcher crashed mid-search) to
    ``pending`` so it can be searched again. Returns how many were reaped."""
    stale_before = _utcnow() - _LEASE_STALE
    res = db.execute(
        update(WorkSourceSearch)
        .where(
            WorkSourceSearch.status == "searching",
            (WorkSourceSearch.leased_at.is_(None)) | (WorkSourceSearch.leased_at < stale_before),
        )
        .values(status="pending", lease_token=None, leased_at=None)
    )
    db.commit()
    return res.rowcount or 0


def reset_sources(db: Session, req: ContentRequest) -> None:
    """Admin "Recheck now": RESET every durable source row to ``pending`` and clear its lease, so a
    forced re-acquire searches every source fresh — the human "try everything" override. Leaves no
    durable source terminal (no_match/exhausted/unavailable all → pending)."""
    if req is None:
        return
    db.execute(
        update(WorkSourceSearch)
        .where(
            WorkSourceSearch.content_request_id == req.id,
            WorkSourceSearch.status.in_(("no_match", "exhausted", "unavailable", "skipped")),
        )
        .values(status="pending", lease_token=None, leased_at=None,
                next_retry_at=None, reason=None)
    )
    db.commit()


def record_attempt(db: Session, source: str, ok: bool) -> None:
    """Append a :class:`SourceAttempt` (one per durable search issued) — powers the availability cap."""
    db.add(SourceAttempt(source=source, ok=ok))
    db.commit()


def _daily_cap(db: Session, source: str) -> int | None:
    """The opt-in per-source daily search cap from ``Integration.config.max_daily_requests`` for the
    integration backing ``source``. None = uncapped (the default — backoff-only)."""
    kind = _SOURCE_INTEGRATION.get(source)
    if kind is None:
        return None
    integ = db.scalar(select(Integration).where(
        Integration.kind == kind, Integration.enabled.is_(True)))
    if integ is None:
        return None
    cap = (integ.config or {}).get("max_daily_requests")
    try:
        cap = int(cap)
    except (TypeError, ValueError):
        return None
    return cap if cap > 0 else None


def source_available_now(db: Session, source: str) -> bool:
    """Is ``source`` under its daily search cap right now? True (always) when uncapped; else
    ``count(SourceAttempt in the last 24h) < cap``."""
    cap = _daily_cap(db, source)
    if cap is None:
        return True
    since = _utcnow() - _DAY
    n = db.scalar(select(func.count(SourceAttempt.id)).where(
        SourceAttempt.source == source, SourceAttempt.created_at > since)) or 0
    return n < cap


def next_source_free_at(db: Session, source: str) -> datetime | None:
    """When ``source`` next drops below its daily cap (``_grab_blocked_until`` math: the oldest
    in-window attempt that must age out, + 24h). None when uncapped or already under cap."""
    cap = _daily_cap(db, source)
    if cap is None:
        return None
    since = _utcnow() - _DAY
    times = db.scalars(
        select(SourceAttempt.created_at)
        .where(SourceAttempt.source == source, SourceAttempt.created_at > since)
        .order_by(SourceAttempt.created_at)
    ).all()
    if len(times) < cap:
        return None
    return _aware(times[len(times) - cap]) + _DAY
