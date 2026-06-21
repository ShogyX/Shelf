"""Missing-content ledger — a per-TITLE record of content that was REQUESTED but NOT FOUND.

A title can be unavailable through every route (no usenet release, no open-library match, all
candidates broken, the endpoint blocked …). Without a memory of that, the app re-searches the same
dead title on every request/stock pass, hammering Prowlarr/SABnzbd/libgen for something known to be
unobtainable. This ledger fixes that:

* :func:`note_request` records who wants a title (the requester join), opening a row if new.
* :func:`mark_unavailable` records that every route failed, schedules a JITTERED re-check, and is
  what :func:`is_gated` then reads to SKIP further searches until the re-check is due.
* :func:`mark_resolved` clears the gate the moment the title is successfully imported/stocked.

Keyed by ``norm_key`` + media bucket (text | comic) — the same cluster identity the catalog and the
download dedup use, so the whole logical title is gated/resolved as one (not per catalog row).

Complementary to :class:`app.models.BrokenRelease`, which is per-RELEASE (one dead NZB/mirror link);
this is per-TITLE (the title couldn't be obtained by any route).
"""
from __future__ import annotations

import logging
import random
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import config_store
from ..models import CatalogWork, ContentRequest, ContentRequestRequester

log = logging.getLogger("shelf.ledger")

# The failure_reason vocabulary the routes map their exhaustion to (see ContentRequest.failure_reason).
REASONS = ("no_match", "all_broken", "rate_limited", "blocked", "unverified", "timeout", "error")
# Transient reasons mean the provider was temporarily unreachable (Cloudflare/429/timeout), NOT that
# the title is genuinely unavailable — the existing per-provider backoff already retries those. These
# get a SHORT re-check so we don't lock a recoverable title out for the full 14-day window; only
# PERMANENT reasons (no_match/all_broken/unverified/error) get the long jittered interval.
_TRANSIENT_REASONS = frozenset({"rate_limited", "blocked", "timeout"})
_TRANSIENT_RECHECK = timedelta(hours=6)
# Jitter the re-check ±25% of the interval, so a batch of titles marked unavailable in the same
# minute don't all come due together (which would re-flood the services the moment they're due).
_JITTER_FRACTION = 0.25


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _bucket(cw: CatalogWork) -> str:
    return "comic" if (cw.media_kind or "text") == "comic" else "text"


def _next_check_at(now: datetime | None = None) -> datetime:
    """When the periodic tick should re-try an unavailable title: ``now + interval ± jitter``.

    interval = ``missing_recheck_days`` (admin-editable, default 14) days. jitter spreads the due
    time across ``[interval*0.75, interval*1.25]`` — for the 14-day default that's roughly ±3.5 days,
    so a burst of titles marked unavailable at the same instant fan out over a week-wide window
    instead of all coming due in the same tick. Combined with the per-tick batch cap
    (``missing_recheck_batch``) and the ~30-min cadence, this bounds re-check request volume."""
    now = now or _utcnow()
    interval_s = max(1, int(config_store.effective("missing_recheck_days"))) * 86400
    factor = 1.0 + random.uniform(-_JITTER_FRACTION, _JITTER_FRACTION)
    return now + timedelta(seconds=interval_s * factor)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


# Keys in CatalogWork.extra that may carry a full provider release/pub date (parsed leniently — a
# bare YYYY is treated as Jan 1 of that year via the year fallback, not here).
_RELEASE_DATE_KEYS = ("release_date", "publish_date", "pub_date", "first_publish_date",
                      "published_date", "publishedDate")


def _parse_date(val) -> date | None:
    """A full (Y-M-D) date parsed from a provider string/date, else None. A bare year (``"2027"`` or
    an int) is NOT a full date here — that's the ``cw.year`` fallback's job in ``_planned_until``."""
    if isinstance(val, date):
        return val
    if not isinstance(val, str):
        return None
    s = val.strip()[:10]
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


def _planned_until(cw: CatalogWork) -> date | None:
    """The FUTURE release date of a not-yet-released ("Planned") title, else None.

    LOCKED DECISION: a title is Planned only when a provider release date/year is in the FUTURE; an
    UNKNOWN date is treated as Released (never blocks a fetchable title). So:
    * a parseable full release/pub date in ``cw.extra`` that's in the future → that date;
    * elif ``cw.year`` is in a future calendar year → Jan 1 of that year;
    * else None (released, or unknown — NEVER planned on missing data)."""
    today = datetime.now(UTC).date()
    extra = cw.extra if isinstance(cw.extra, dict) else {}
    for key in _RELEASE_DATE_KEYS:
        d = _parse_date(extra.get(key))
        if d is not None:
            return d if d > today else None
    if cw.year and cw.year > today.year:
        return date(cw.year, 1, 1)
    return None


def _get(db: Session, cw: CatalogWork) -> ContentRequest | None:
    if not cw.norm_key:
        return None
    return db.scalar(select(ContentRequest).where(
        ContentRequest.norm_key == cw.norm_key,
        ContentRequest.media_bucket == _bucket(cw),
    ))


def _upsert(db: Session, cw: CatalogWork) -> ContentRequest | None:
    """Get (or create, racing-safe) the ledger row for ``cw``'s cluster. Returns None for an
    untitled row (empty norm_key would collide every untitled title into one bucket)."""
    if not cw.norm_key:
        return None
    row = _get(db, cw)
    if row is not None:
        return row
    row = ContentRequest(
        norm_key=cw.norm_key, media_bucket=_bucket(cw), catalog_work_id=cw.id,
        title=(cw.title or cw.norm_key)[:512], author=cw.author, status="open",
    )
    db.add(row)
    try:
        db.commit()
    except IntegrityError:                # raced another path creating the same cluster row
        db.rollback()
        row = _get(db, cw)
    if row is not None:
        db.refresh(row)
    return row


def _attach_requester(db: Session, row: ContentRequest, user_id: int | None) -> None:
    """Record that ``user_id`` (NULL = system/stock) wants this title. Idempotent per (row, user)."""
    exists = db.scalar(select(ContentRequestRequester.id).where(
        ContentRequestRequester.request_id == row.id,
        ContentRequestRequester.user_id.is_(None) if user_id is None
        else ContentRequestRequester.user_id == user_id,
    ))
    if exists is not None:
        return
    db.add(ContentRequestRequester(request_id=row.id, user_id=user_id))
    try:
        db.commit()
    except IntegrityError:                # raced another request from the same user
        db.rollback()


def note_request(db: Session, cw: CatalogWork, user_id: int | None, *,
                 origin: str | None = None, origin_detail: str | None = None) -> ContentRequest | None:
    """Open (or reuse) the ledger row for ``cw``'s title and attach ``user_id`` as a requester.
    Does NOT change a row already marked unavailable/resolved — just records the new requester.

    ``origin``/``origin_detail`` tag HOW the row entered the ledger (e.g. "series" + the series name,
    set by the auto-series hook). Only stamped on a row this call CREATES — a row that already exists
    (a direct request, or an earlier sibling) keeps its origin, never overwritten by a later auto-pull."""
    is_new = origin is not None and _get(db, cw) is None
    row = _upsert(db, cw)
    if row is not None:
        if is_new and origin:
            row.origin = origin
            row.origin_detail = origin_detail
            db.commit()
        _attach_requester(db, row, user_id)
    return row


def mark_unavailable(db: Session, cw: CatalogWork, reason: str | None = None,
                     provider: str | None = None) -> ContentRequest | None:
    """Every route failed for this title: open/reuse the row, bump attempts, record the reason +
    provider, and schedule a JITTERED re-check (which then GATES further searches until it's due)."""
    row = _upsert(db, cw)
    if row is None:
        return None
    now = _utcnow()
    row.status = "unavailable"
    row.failure_reason = reason if reason in REASONS else (reason or "error")
    row.last_provider = (provider or row.last_provider)
    row.attempts = (row.attempts or 0) + 1
    row.last_attempt_at = now
    # Transient block → short retry (don't 14-day-lock a recoverable title); permanent → full interval.
    row.next_check_at = (now + _TRANSIENT_RECHECK
                         if row.failure_reason in _TRANSIENT_REASONS else _next_check_at(now))
    row.resolved_at = None
    if row.catalog_work_id is None:
        row.catalog_work_id = cw.id
    db.commit()
    return row


def mark_planned(db: Session, cw: CatalogWork, release_date: date) -> ContentRequest | None:
    """The title isn't released yet (a future provider date) → mark the row ``planned`` and stamp the
    release date. The re-evaluation sweep in ``source_retry_tick`` flips it back to ``open`` (and lets
    the normal recheck/acquire path search it) once ``release_date`` passes. No search runs meanwhile."""
    row = _upsert(db, cw)
    if row is None:
        return None
    row.status = "planned"
    row.release_date = release_date
    row.next_check_at = None
    row.resolved_at = None
    if row.catalog_work_id is None:
        row.catalog_work_id = cw.id
    db.commit()
    # Drop any stale per-source children from a prior failed search → out of the retry tick's
    # `due_unavailable` queue, so a planned title doesn't burn the per-source retry budget every tick
    # (the sweep resets them again on the way back to `open` when it releases).
    from . import source_state
    source_state.reset_sources(db, row)
    return row


def mark_resolved(db: Session, cw: CatalogWork, source: str | None = None) -> ContentRequest | None:
    """The title was successfully imported/stocked → clear the gate. No-op if there's no ledger row
    (the common case: a title that was found first try was never recorded).

    R20 (Wave B): a REAL import is the ONLY place the per-source ``unavailable`` queue is dropped — the
    OTHER sources' pending transient retries are now moot, so they're marked ``skipped``. ``source`` is
    the route that imported (left untouched). Passed only by the genuine import/hook hooks, never the
    acquire-time match."""
    row = _get(db, cw)
    if row is None:
        return None
    row.status = "resolved"
    row.resolved_at = _utcnow()
    row.next_check_at = None
    db.commit()
    from . import source_state
    source_state.drop_upstream_unavailable(db, row, keep_source=source)
    return row


def is_gated(db: Session, cw: CatalogWork) -> tuple[bool, datetime | None]:
    """Should searches/grabs for this title be SKIPPED right now? True iff a row exists that is either
    ``unavailable`` with a future next_check_at, OR ``planned`` (its provider release date hasn't passed
    — the gate's next-check is that release date). Returns ``(gated, next_check_at)``."""
    row = _get(db, cw)
    if row is None:
        return (False, None)
    if row.status == "planned":
        # The next check is the release date (midnight UTC of that day); searching a future book is
        # futile until then. A planned row with NO release_date can never un-plan (the sweep requires
        # release_date NOT NULL) — treat it as released/searchable so it isn't gated forever.
        rd = row.release_date
        if rd is None:
            return (False, None)
        nca = datetime(rd.year, rd.month, rd.day, tzinfo=UTC)
        return (True, nca)
    if row.status != "unavailable":
        return (False, None)
    nca = _aware(row.next_check_at)
    if nca is not None and nca > _utcnow():
        return (True, nca)
    return (False, nca)
