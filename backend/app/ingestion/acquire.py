"""Acquisition routing — pick HOW to obtain a catalog work.

A logical work can be obtainable several ways: crawled from a web-index source (hook), pulled by a
connected library manager (Readarr/Kapowarr grab), or downloaded via the usenet pipeline
(Prowlarr→SABnzbd). The operator sets a default priority order; each user may override it; and a
user may pick a specific route per title. Manual acquisition and auto-fetch (Goodreads / catalog)
both resolve a title down the same priority list.
"""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import AppSetting, CatalogWork, IndexSite, Integration
from .outcome import Outcome, RouteResult

log = logging.getLogger("shelf.acquire")

# Severity order for picking the most informative non-matched reason to surface as `detail`.
_OUTCOME_RANK = {
    Outcome.NO_MATCH: 0, Outcome.EXHAUSTED: 1, Outcome.UNAVAILABLE: 2, Outcome.ERROR: 3,
}

ROUTES = ("torrent", "pipeline", "libgen", "web_index", "readarr", "kapowarr")
# Default order: torrents FIRST (exhaustively), then the usenet pipeline, then the Anna's Archive
# (libgen) direct-download fallback. Each is tried only if configured; the cascade exhausts one
# route's candidates before the next. Operators/users can reorder this on the Acquisition page.
DEFAULT_PRIORITY = ["torrent", "pipeline", "libgen", "web_index", "readarr", "kapowarr"]
_GLOBAL_KEY = "fetch_source_priority"


def _clean(order) -> list[str]:
    """Normalize a priority list: drop unknown/duplicate routes, then fill in any the caller omitted
    so resolution always has a full fallback chain. An omitted route is inserted at its
    DEFAULT_PRIORITY-relative slot (NOT appended last), so a route added after a user saved their
    order — e.g. ``torrent`` on an install whose priority was set before torrents existed — takes its
    intended high-priority position instead of silently falling to the back of the chain."""
    rank = {r: i for i, r in enumerate(DEFAULT_PRIORITY)}
    seen, out = set(), []
    for r in order or []:
        if r in ROUTES and r not in seen:
            seen.add(r)
            out.append(r)
    for r in DEFAULT_PRIORITY:
        if r in seen:
            continue
        # Insert before the first already-present route that is lower-priority by default (a higher
        # default rank), preserving the user's explicit relative ordering of the routes they did list.
        pos = next((i for i, e in enumerate(out) if rank[e] > rank[r]), len(out))
        out.insert(pos, r)
        seen.add(r)
    return out


def global_priority(db: Session) -> list[str]:
    row = db.get(AppSetting, _GLOBAL_KEY)
    return _clean(row.value if row and isinstance(row.value, list) else None)


def set_global_priority(db: Session, order: list[str]) -> list[str]:
    val = _clean(order)
    row = db.get(AppSetting, _GLOBAL_KEY)
    if row is None:
        db.add(AppSetting(key=_GLOBAL_KEY, value=val))
    else:
        row.value = val
    db.commit()
    return val


def _user_key(user_id: int) -> str:
    return f"{_GLOBAL_KEY}:user:{user_id}"


def user_priority(db: Session, user) -> list[str]:
    """A user's effective route priority: their override, else the global default."""
    if user is not None:
        row = db.get(AppSetting, _user_key(user.id))
        if row and isinstance(row.value, list):
            return _clean(row.value)
    return global_priority(db)


def set_user_priority(db: Session, user_id: int, order: list[str] | None) -> list[str]:
    """Set (or clear, with None) a user's override. Returns the effective list."""
    key = _user_key(user_id)
    row = db.get(AppSetting, key)
    if order is None:
        if row is not None:
            db.delete(row)
        db.commit()
        return global_priority(db)
    val = _clean(order)
    if row is None:
        db.add(AppSetting(key=key, value=val))
    else:
        row.value = val
    db.commit()
    return val


def _members(db: Session, rep: CatalogWork) -> list[CatalogWork]:
    """The catalog rows clustered with `rep` (same normalized title + media class)."""
    if not rep.norm_key:  # empty key would match every untitled row — just use this one
        return [rep]
    bucket = "comic" if (rep.media_kind or "text") == "comic" else "text"
    rows = db.scalars(
        select(CatalogWork).where(CatalogWork.norm_key == rep.norm_key)
    ).all()
    same = [r for r in rows if ("comic" if (r.media_kind or "text") == "comic" else "text") == bucket]
    return same or [rep]


def crawled_match_ok(db: Session, row: CatalogWork, want_kind: str | None) -> bool:
    """Strict content-type gate for a CRAWLED (web_index) catalog row. A crawl source only serves the
    media kinds DEFINED for it (``IndexSite.allowed_media_kinds``), so `row` may match a request only
    when both the row's kind and the requested kind are served by that site, and the requested medium is
    compatible with the row's (prose vs comic never cross). This is the single place the source's
    content-type definition is enforced — applied both at MATCH time (series/_pick_by_author) and at
    ACQUIRE time (_web_index_ok) so a wrong-type crawl entry can never be matched or hooked.

    Non-crawled rows are not gated here (the download routes type-rank those) → returns True. An UNKNOWN
    requested kind skips the want-vs-row comparison (nothing to compare) but the site's own allowlist is
    still enforced on the row's kind."""
    if row.provider != "web_index" or row.site_id is None:
        return True
    from . import matchmeta as mm
    site = db.get(IndexSite, row.site_id)
    allowed = (site.allowed_media_kinds if site else None) or None
    row_kind = (row.media_kind or "text")
    if allowed and row_kind not in allowed:
        return False
    if want_kind:
        if allowed and want_kind not in allowed:
            return False   # this crawl source doesn't serve the requested kind at all
        if mm.bucket_of(None, media_kind=want_kind) != mm.bucket_of(None, media_kind=row_kind):
            return False   # wrong medium — e.g. a comic request vs a prose crawl entry
    return True


def _web_index_ok(db: Session, rep: CatalogWork, m: CatalogWork) -> bool:
    """Whether a web_index catalog member `m` is a valid match for the requested work `rep`.

    web_index clusters by normalized TITLE only, so a same-title different-author entry — e.g. a
    web-novel "Necromancer" by "Pig On A Journey" matched against Terry Mancour's "Necromancer" — is a
    false positive. Require author COMPATIBILITY when both rows carry an author (missing author on
    either side can't be checked, so it's allowed — don't over-reject on sparse crawl metadata), and
    enforce the source's content-type definition via ``crawled_match_ok`` (want_kind = rep's kind)."""
    from .extract import authors_compatible
    if rep.author and m.author and not authors_compatible(rep.author, m.author):
        return False
    return crawled_match_ok(db, m, rep.media_kind)


def pipeline_configured(db: Session) -> bool:
    """True when the Prowlarr+SABnzbd acquisition pipeline is fully set up (both enabled). Books
    from googlebooks/openlibrary/hardcover can ONLY be acquired through this pipeline, so the Index
    hides those catalog items when it returns False."""
    sab = db.scalar(select(Integration.id).where(
        Integration.kind == "sabnzbd", Integration.enabled.is_(True)))
    prow = db.scalar(select(Integration.id).where(
        Integration.kind == "prowlarr", Integration.enabled.is_(True)))
    return sab is not None and prow is not None


def available_routes(db: Session, rep: CatalogWork) -> list[str]:
    """Which routes can actually fulfill this work right now (for the UI's route picker)."""
    members = _members(db, rep)
    out: list[str] = []
    if any(m.provider == "web_index" and m.hooked_work_id is None for m in members):
        out.append("web_index")
    for kind in ("readarr", "kapowarr"):
        if any(m.provider == kind and m.integration_id for m in members):
            out.append(kind)
    from . import torrents
    if torrents.configured(db):     # Prowlarr torrent indexers + qBittorrent
        out.append("torrent")
    if pipeline_configured(db):
        out.append("pipeline")
    from . import libgen
    if libgen.configured(db):       # any book can be tried via the open-library fallback
        out.append("libgen")
    return out


# Routes that can fulfil an AUDIOBOOK: the download pipelines (torrent/usenet, audio-categorized) and
# the public-domain LibriVox fetcher. Crawl/manager routes (web_index/readarr/kapowarr) and Anna's
# Archive (libgen, ebook-only) never serve audiobooks, so an audiobook request skips them.
AUDIO_ROUTES = ("torrent", "pipeline", "librivox")


async def acquire(
    db: Session, rep: CatalogWork, *, user_id: int | None, priority: list[str],
    shelf_id: int | None = None, route: str | None = None, context: dict | None = None,
    force: bool = False, variant: str = "ebook",
) -> dict:
    """Acquire `rep`'s work via the first route (in `priority`, or just `route` if forced) that can
    fulfill it. Returns {"route", "status", ...}. ``status``: hooked | grabbed | downloading | none |
    gated.

    ``variant="audiobook"`` fetches the AUDIOBOOK of the title (a SEPARATE Work) via the audio-capable
    routes only; it bypasses the 'already hooked' short-circuit + the missing-content ledger (those
    track the ebook), since an audiobook is independent of whether the ebook is in the library.

    The missing-content ledger GATES titles already known to be unavailable: a normal request for a
    gated title does NOT search (it just attaches the requester and returns ``gated``) until its
    periodic re-check is due. ``force=True`` (admin / the re-check tick) bypasses the gate."""
    from . import catalog, downloads, ledger, source_state
    from ..integrations import sync as isync
    from ..library import add_to_library

    audiobook = variant == "audiobook"

    if rep.hooked_work_id is not None and not audiobook:
        if user_id:
            add_to_library(db, user_id, rep.hooked_work_id, shelf_id=shelf_id)
        ledger.mark_resolved(db, rep)  # already in the library → clear any stale gate
        return {"route": "library", "status": "hooked", "work_id": rep.hooked_work_id}

    # Record who wants this title (opens a ledger row if new); then honor the gate unless forced.
    # The ledger tracks ebook availability, so audiobook requests don't touch it (v1).
    if not audiobook:
        ledger.note_request(db, rep, user_id,
                            origin=(context or {}).get("origin"),
                            origin_detail=(context or {}).get("origin_detail"))
        # Released/Planned gate: a title whose provider release date/year is in the FUTURE is "planned"
        # and is NOT searched (searching a future book is futile) — this applies EVEN under force. An
        # unknown/past date is Released and never blocks a fetchable title. The re-evaluation sweep in
        # source_retry_tick flips a planned row to "open" + searches it once its release date passes.
        planned_until = ledger._planned_until(rep)
        if planned_until is not None:
            ledger.mark_planned(db, rep, planned_until)
            return {"route": None, "status": "planned", "release_date": planned_until.isoformat()}
        if not force:
            gated, next_check = ledger.is_gated(db, rep)
            if gated:
                return {"route": None, "status": "gated",
                        "next_check_at": next_check.isoformat() if next_check else None}

    members = _members(db, rep)
    order = [route] if route else priority
    if audiobook:  # only the audio-capable routes can fulfil an audiobook
        order = [r for r in order if r in AUDIO_ROUTES]
        # LibriVox isn't in the configurable route priority (it's audiobook-only); append it as the
        # public-domain fallback after the pipelines, unless a specific route was forced.
        if route is None and "librivox" not in order:
            order.append("librivox")

    # Wave B per-source search state: the ledger row + a `pending` child row per durable source in
    # this cascade (torrent/pipeline/libgen). The audiobook path doesn't touch the ledger (it tracks
    # the ebook), so it has no per-source rows either. `terminal` is the no_match/exhausted skip-set
    # (R22) — honored even under `force`; an admin "recheck now" RESETs those to pending FIRST.
    req = None if audiobook else ledger._get(db, rep)
    terminal: set[str] = set()
    if req is not None:
        source_state.ensure_rows(db, req, [r for r in order if r in source_state.DURABLE_SOURCES])
        terminal = source_state.terminal_sources(db, req)

    # Each route block builds a RouteResult (internal plumbing) instead of mutating a `last_err`
    # string: on a match it carries the public dict's pieces and we return that dict UNCHANGED; a
    # non-match is collected so the worst reason can be threaded into the response detail. The bottom
    # ledger gating (CODE-H1) is unchanged — it still keys only on `route is None and not audiobook`.
    results: list[RouteResult] = []
    def _lease_durable(r: str) -> bool:
        """Per-source gate for a durable download source about to be searched: skip a TERMINAL source
        (R22, even under force), else CAS-lease its row so a concurrent retry tick + this live acquire
        never double-search the same source. Returns True to proceed (leased), False to skip the route
        this pass (terminal, or another searcher holds it). No-op (True) when there's no ledger row."""
        if req is None:
            return True
        if r in terminal:
            return False
        return source_state.lease(db, req, r) is not None

    def _record_source(r: str, oc: Outcome, *, reason: str | None = None) -> None:
        """Persist the per-source search result + a SourceAttempt (Wave B). Maps the route's Outcome
        to the durable-source status:
          MATCHED→matched · NO_MATCH→no_match (terminal) · UNAVAILABLE/ERROR→unavailable (retried).
        EXHAUSTED is never produced at acquire-time (it's a worker-time verdict, written from the
        download hooks). Releases the lease either way. No-op when there's no ledger row."""
        if req is None:
            return
        from datetime import timedelta
        if oc is Outcome.MATCHED:
            source_state.record(db, req, r, "matched")
            source_state.record_attempt(db, r, ok=True)
            return
        if oc is Outcome.NO_MATCH:
            source_state.record(db, req, r, "no_match", reason=reason)
            source_state.record_attempt(db, r, ok=True)
            return
        # UNAVAILABLE / ERROR: transient — schedule a per-source retry. A quota (rate_limited) hit
        # waits until the source next drops below its daily cap; everything else gets the fixed 6h
        # transient re-check (ledger._TRANSIENT_RECHECK).
        now = source_state._utcnow()
        retry_at = None
        if reason == "rate_limited":
            retry_at = source_state.next_source_free_at(db, r)
        retry_at = retry_at or (now + ledger._TRANSIENT_RECHECK)
        source_state.record(db, req, r, "unavailable", reason=reason, retry_at=retry_at)
        source_state.record_attempt(db, r, ok=False)

    for r in order:
        if r == "web_index":
            cand = next((m for m in members if m.provider == "web_index" and m.hooked_work_id is None
                         and _web_index_ok(db, rep, m)), None)
            if cand is None:
                continue
            try:
                work = await catalog.hook_entry(db, cand)
            except Exception as exc:  # noqa: BLE001 — try the next route
                results.append(RouteResult(Outcome.ERROR, route=r, reason=f"web_index: {exc}"))
                continue
            if user_id:
                add_to_library(db, user_id, work.id, shelf_id=shelf_id)
            ledger.mark_resolved(db, rep)
            return {"route": "web_index", "status": "hooked", "work_id": work.id}

        if r in ("readarr", "kapowarr"):
            cand = next((m for m in members if m.provider == r and m.integration_id), None)
            if cand is None:
                continue
            try:
                await isync.grab_external(db, cand)
                ledger.mark_resolved(db, rep)
                return {"route": r, "status": "grabbed", "catalog_id": cand.id}
            except Exception as exc:  # noqa: BLE001
                results.append(RouteResult(Outcome.ERROR, route=r, reason=f"{r}: {exc}"))
                continue

        if r == "torrent":
            from . import torrents
            if not torrents.configured(db):
                continue
            if not _lease_durable(r):
                continue
            from . import release_matcher as _rm
            _rm.reset_search_failure()
            try:
                job = await torrents.grab(db, rep, user_id=user_id, shelf_id=shelf_id,
                                          context=context, variant=variant)
            except Exception as exc:  # noqa: BLE001 — try the next route
                # An "infra" raise (no qBittorrent downloader) is a transient UNAVAILABLE; any other
                # raise is an ERROR. Either way the loop continues to the next route, as before.
                oc = Outcome.UNAVAILABLE if "qbittorrent" in str(exc).lower() else Outcome.ERROR
                _record_source(r, oc, reason=f"torrent: {exc}")
                results.append(RouteResult(oc, route=r, reason=f"torrent: {exc}"))
                continue
            if job is not None:
                _record_source(r, Outcome.MATCHED)
                return {"route": "torrent", "status": "downloading", "job_id": job.id}
            fail = _rm.last_search_failure()   # B-min: an all-failed Prowlarr search is an outage
            _record_source(r, Outcome.UNAVAILABLE if fail else Outcome.NO_MATCH, reason=fail)
            results.append(RouteResult(Outcome.NO_MATCH, route=r,
                                       reason="torrent: no confident release match"))

        if r == "pipeline":
            if "pipeline" not in available_routes(db, rep):
                continue
            if not _lease_durable(r):
                continue
            # Drive the Prowlarr match from the SELECTED row's own title/author — a same-norm_key
            # cluster can contain wrong-author editions (e.g. study guides), so picking an arbitrary
            # member would search against the wrong author and find nothing.
            cw = rep
            from . import release_matcher as _rm
            _rm.reset_search_failure()
            try:
                job = await downloads.auto_grab(db, cw, user_id=user_id, shelf_id=shelf_id,
                                                context=context, variant=variant)
            except Exception as exc:  # noqa: BLE001
                oc = Outcome.UNAVAILABLE if "sabnzbd" in str(exc).lower() else Outcome.ERROR
                _record_source(r, oc, reason=f"pipeline: {exc}")
                results.append(RouteResult(oc, route=r, reason=f"pipeline: {exc}"))
                continue
            if job is not None:
                _record_source(r, Outcome.MATCHED)
                return {"route": "pipeline", "status": "downloading", "job_id": job.id}
            fail = _rm.last_search_failure()   # B-min: an all-failed Prowlarr search is an outage
            _record_source(r, Outcome.UNAVAILABLE if fail else Outcome.NO_MATCH, reason=fail)
            results.append(RouteResult(Outcome.NO_MATCH, route=r,
                                       reason="pipeline: no confident release match"))

        if r == "libgen":
            from . import libgen
            if not libgen.configured(db):
                continue
            if not _lease_durable(r):
                continue
            try:
                job = await libgen.grab(db, rep, user_id=user_id, shelf_id=shelf_id, context=context)
            except Exception as exc:  # noqa: BLE001
                _record_source(r, Outcome.ERROR, reason=f"libgen: {exc}")
                results.append(RouteResult(Outcome.ERROR, route=r, reason=f"libgen: {exc}"))
                continue
            if job is not None:
                _record_source(r, Outcome.MATCHED)
                return {"route": "libgen", "status": "downloading", "job_id": job.id}
            _record_source(r, Outcome.NO_MATCH)
            results.append(RouteResult(Outcome.NO_MATCH, route=r,
                                       reason="libgen: no open-library match found"))

        if r == "librivox":
            from . import librivox
            try:
                job = await librivox.grab(db, rep, user_id=user_id, shelf_id=shelf_id, context=context)
            except Exception as exc:  # noqa: BLE001
                results.append(RouteResult(Outcome.ERROR, route=r, reason=f"librivox: {exc}"))
                continue
            if job is not None:
                return {"route": "librivox", "status": "downloading", "job_id": job.id}
            results.append(RouteResult(Outcome.NO_MATCH, route=r,
                                       reason="librivox: no public-domain audiobook match"))

    # No route could even START fulfilling this title (no web hook, no manager grab, pipeline/libgen
    # not configured or found nothing to enqueue) → record it unavailable so it's gated + re-checked.
    # An in-flight pipeline/libgen download returns "downloading" above; its own exhaustion/import
    # hook (downloads/_grab_next, libgen/_advance_job, _import_*) updates the ledger when it lands.
    # ONLY gate when the FULL priority chain was tried — a forced single ``route`` that found nothing
    # must not mark the whole title unavailable (it would gate every OTHER route too, CODE-H1). The
    # ledger tracks the ebook, so an audiobook miss never gates the title.
    if route is None and not audiobook:
        ledger.mark_unavailable(db, rep, reason="no_match", provider=None)
    # The detail surfaces the WORST non-matched outcome's reason (an ERROR/UNAVAILABLE is more
    # informative than a plain NO_MATCH); None when no route even ran (matching the old `last_err`).
    worst = max(results, key=lambda rr: _OUTCOME_RANK[rr.outcome], default=None)
    return {"route": None, "status": "none", "detail": worst.reason if worst else None}
