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
    """The effective acquisition route priority. This is now GLOBAL-ONLY + admin-controlled: every
    acquisition (user- or system-initiated) uses the operator's single global order. Per-user
    overrides are no longer honoured (the ``user`` arg is kept so the many call sites don't churn,
    and legacy override rows are still purged on user delete via ``set_user_priority``/``_user_key``)."""
    return global_priority(db)


def set_user_priority(db: Session, user_id: int, order: list[str] | None) -> list[str]:
    """Legacy per-user override storage (NO LONGER honoured by ``user_priority`` — acquisition order
    is global-only). Retained so a deleted user's stray override row is still cleaned up. Set (or
    clear, with None) the row; returns the current effective (global) list."""
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
    return global_priority(db)  # override is stored but no longer effective (global-only)


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


# Metadata providers: a request originating from one of these is a CATALOGUED title with a real,
# known author/identity — as opposed to a web_index crawl listing (clustered by bare title). A
# crawl source like novellunar must not be allowed to fulfil a metadata-provider title unless it
# genuinely corroborates that author (see _web_index_ok).
_META_PROVIDERS = frozenset({"hardcover", "openlibrary", "googlebooks"})


def _web_index_ok(db: Session, rep: CatalogWork, m: CatalogWork,
                  meta_author: str | None = None) -> bool:
    """Whether a web_index catalog member `m` is a valid match for the requested work `rep`.

    web_index clusters by normalized TITLE only, so a same-title different-author entry — e.g. a
    web-novel "Necromancer" by "Pig On A Journey" matched against Terry Mancour's "Necromancer" — is a
    false positive.

    AUTHOR gate, with the lenience scoped to web_index→web_index matches only:
      * When the request comes from a METADATA PROVIDER (hardcover/openlibrary/googlebooks) for which
        a real author is known — ``meta_author`` (the cluster's catalogued author, which may be richer
        than ``rep.author`` when the picked metadata row itself is authorless) — a web_index crawl row
        may match ONLY if it carries a COMPATIBLE author. A crawl entry whose author is MISSING or
        incompatible is rejected: a novel-only crawl source (novellunar) must never fetch a
        hardcover/OL/GB title it can't corroborate. (Root cause of the Spellmonger "Necromancer" →
        novellunar false-hook.)
      * For a web_index→web_index match (web novel to web novel, no metadata author to check) keep the
        original lenient rule: reject only when BOTH rows carry an author and they're incompatible —
        don't over-reject on sparse crawl metadata.

    Then enforce the source's content-type definition via ``crawled_match_ok`` (want_kind = rep's kind)."""
    from .extract import authors_compatible
    want_author = meta_author or (rep.author if rep.provider in _META_PROVIDERS else None)
    if rep.provider in _META_PROVIDERS and want_author:
        # Metadata-provider request with a known author → the crawl row MUST corroborate it.
        if not m.author or not authors_compatible(want_author, m.author):
            return False
    elif rep.author and m.author and not authors_compatible(rep.author, m.author):
        return False  # web_index→web_index: only reject a known, conflicting author pair
    return crawled_match_ok(db, m, rep.media_kind)


def _meta_author(rep: CatalogWork, members: "list[CatalogWork]") -> str | None:
    """The catalogued author across `rep`'s metadata-provider members — lets a web_index row be
    corroborated even when the picked rep is an authorless metadata edition (a googlebooks "Necromancer"
    with no author, whose sibling hardcover row knows it's Terry Mancour). None for a non-metadata rep."""
    if rep.provider not in _META_PROVIDERS:
        return None
    return next((m.author for m in members if m.provider in _META_PROVIDERS and m.author), None)


def web_index_member(db: Session, rep: CatalogWork, members: "list[CatalogWork]") -> "CatalogWork | None":
    """The first acquirable web_index member that PASSES the author/content gate for `rep` — the single
    place 'can a crawl source fulfil this title?' is decided, shared by acquire + available_routes so the
    route picker can never offer a web_index source the acquire would then reject."""
    ma = _meta_author(rep, members)
    return next((m for m in members if m.provider == "web_index" and m.hooked_work_id is None
                 and _web_index_ok(db, rep, m, ma)), None)


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
    if web_index_member(db, rep, members) is not None:   # gated: only a same-author/-content crawl row
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


# Routes whose acquisitions EXPAND to every configured content language × format (EN/NO ×
# ebook/audiobook). Deliberately only the download pipelines — the web-crawl route serves exactly
# what its source carries, and the library managers own their own format/language logic.
EXPAND_ROUTES = ("pipeline", "libgen", "torrent")


def _language_members(db: Session, rep: CatalogWork) -> dict[str, CatalogWork]:
    """Same-cluster catalog rows by LANGUAGE BUCKET (one representative per bucket, the rep's own
    bucket included). A language-pinned acquisition just acquires the member in that language —
    its ``language`` column then drives the whole chain consistently (search scoring, post-download
    verify, dedup bucket) with no parameter threading."""
    from . import language as _lang
    conds = []
    if rep.norm_key:
        conds.append(CatalogWork.norm_key == rep.norm_key)
    if rep.identity_key:
        conds.append(CatalogWork.identity_key == rep.identity_key)
    if not conds:
        return {_lang.bucket(rep.language): rep}
    from sqlalchemy import or_
    rows = db.scalars(select(CatalogWork).where(or_(*conds))).all()
    out: dict[str, CatalogWork] = {}
    for r in rows:
        out.setdefault(_lang.bucket(r.language), r)
    out[_lang.bucket(rep.language)] = rep     # the rep represents its own bucket
    return out


async def expand_variants(db: Session, rep: CatalogWork, *, user_id: int | None,
                          priority: list[str], shelf_id: int | None = None,
                          context: dict | None = None, done: str = "ebook") -> None:
    """Ensure EVERY configured content language × format combination of ``rep``'s title is tracked,
    after one combination was acquired via a download route (usenet pipeline / Anna's-libgen /
    torrent). Each missing combination fires its own ``acquire`` (recursion-guarded), which opens
    its own missing-content ledger row — so the periodic re-check machinery owns the long-term
    retries per combination. Comics only expand across languages (no comic audiobooks).

    Language pinning needs a catalog row IN that language (see _language_members) — when the
    cluster has no row for a configured language, that language is skipped (nothing to search by;
    a later crawl/enrichment that adds the edition makes the next acquisition pick it up)."""
    from .. import config_store
    from . import language as _lang
    from .dedup import edition_exists

    langs = config_store.content_languages()
    members = _language_members(db, rep)
    formats = ("ebook",) if (rep.media_kind or "text") == "comic" else ("ebook", "audiobook")
    for lang in langs:
        member = members.get(_lang.bucket(lang))
        if member is None:
            log.debug("variant expansion: no %s-language catalog row for %r — skipped",
                      lang, rep.title)
            continue
        for fmt in formats:
            if member.id == rep.id and fmt == done:
                continue                       # the combination that triggered this expansion
            kind = "audio" if fmt == "audiobook" else (member.media_kind or "text")
            if edition_exists(db, title=member.title, author=member.author,
                              media_kind=kind, lang=lang):
                continue                       # this edition is already in the library
            try:
                await acquire(db, member, user_id=user_id, priority=priority, shelf_id=shelf_id,
                              context=context, variant=fmt, _expand=False)
            except Exception:  # noqa: BLE001 — best-effort; the trigger acquisition stands
                log.exception("variant expansion failed: %r %s/%s", member.title, lang, fmt)


def _spawn_expansion(rep_id: int, *, user_id: int | None, priority: list[str],
                     shelf_id: int | None, context: dict | None, done: str) -> None:
    """Run expand_variants in the BACKGROUND with its own session — a user's acquire click must
    return when THEIR download starts, not after three more route searches for the other
    language/format combinations."""
    import asyncio

    async def _run() -> None:
        from ..db import SessionLocal
        db2 = SessionLocal()
        try:
            rep2 = db2.get(CatalogWork, rep_id)
            if rep2 is not None:
                await expand_variants(db2, rep2, user_id=user_id, priority=priority,
                                      shelf_id=shelf_id, context=context, done=done)
        except Exception:  # noqa: BLE001 — expansion is best-effort
            log.exception("background variant expansion failed for catalog row %s", rep_id)
        finally:
            db2.close()

    asyncio.create_task(_run())


async def acquire(
    db: Session, rep: CatalogWork, *, user_id: int | None, priority: list[str],
    shelf_id: int | None = None, route: str | None = None, context: dict | None = None,
    force: bool = False, variant: str = "ebook", _expand: bool = True,
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

    # Record who wants this title IN THIS FORMAT (opens a ledger row if new); then honor the gate unless
    # forced. Audiobooks now keep their OWN ledger row (variant), so a missing audiobook is gated +
    # re-checked on the same jittered cadence as a missing ebook — independently of the other format.
    ledger.note_request(db, rep, user_id, variant=variant,
                        origin=(context or {}).get("origin"),
                        origin_detail=(context or {}).get("origin_detail"))
    # Released/Planned gate: a title whose provider release date/year is in the FUTURE is "planned" and
    # is NOT searched (searching a future book is futile) — this applies EVEN under force, to both
    # formats (an unreleased title has no ebook OR audiobook yet). An unknown/past date is Released and
    # never blocks. The re-evaluation sweep in source_retry_tick re-opens + searches once it releases.
    planned_until = ledger._planned_until(rep)
    if planned_until is not None:
        ledger.mark_planned(db, rep, planned_until, variant=variant)
        return {"route": None, "status": "planned", "release_date": planned_until.isoformat()}
    if not force:
        gated, next_check = ledger.is_gated(db, rep, variant=variant)
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
    elif route is None and "web_index" in order and web_index_member(db, rep, members) is not None:
        # This title was INDEXED from a web-crawl source (comix.to / webtoons / gutenberg / novellunar).
        # Fetch from that indexed page FIRST, before the global download pipeline. The member is
        # author/content-gated, so this only fires for a genuine same-title match.
        if (rep.media_kind or "text") == "comic":
            # Comics/manga: comix.to / webtoons is the ONLY trustworthy source. The usenet/torrent/libgen
            # routes title-match CBZ/CBR and pull the WRONG content (the "Vagabond" bug), so a
            # crawl-indexed comic is CRAWL-ONLY — no fallback. If the crawl can't fulfil it right now,
            # leave it unstocked (the retry tick re-tries the crawl) rather than fetch junk.
            order = ["web_index"]
        else:
            # Text/other (gutenberg / novellunar): crawl first, but the global pipeline is a legitimate
            # prose fallback.
            order = ["web_index"] + [r for r in order if r != "web_index"]

    # Wave B per-source search state: the ledger row + a `pending` child row per durable source in
    # this cascade (torrent/pipeline). Per-variant now, so an audiobook has its OWN per-source rows +
    # retries. `terminal` is the no_match/exhausted skip-set (R22) — honored even under `force`; an
    # admin "recheck now" RESETs those to pending FIRST.
    req = ledger._get(db, rep, variant)
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
            cand = web_index_member(db, rep, members)   # gated by author + content (see the helper)
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
                if _expand:   # track every configured language × format of this title too
                    _spawn_expansion(rep.id, user_id=user_id, priority=priority,
                                     shelf_id=shelf_id, context=context, done=variant)
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
                if _expand:
                    _spawn_expansion(rep.id, user_id=user_id, priority=priority,
                                     shelf_id=shelf_id, context=context, done=variant)
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
                if _expand:
                    _spawn_expansion(rep.id, user_id=user_id, priority=priority,
                                     shelf_id=shelf_id, context=context, done=variant)
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
    # must not mark the whole title unavailable (it would gate every OTHER route too, CODE-H1). Gated
    # per-variant, so an audiobook miss schedules the AUDIOBOOK's re-check without touching the ebook.
    if route is None:
        ledger.mark_unavailable(db, rep, reason="no_match", provider=None, variant=variant)
    # The detail surfaces the WORST non-matched outcome's reason (an ERROR/UNAVAILABLE is more
    # informative than a plain NO_MATCH); None when no route even ran (matching the old `last_err`).
    worst = max(results, key=lambda rr: _OUTCOME_RANK[rr.outcome], default=None)
    return {"route": None, "status": "none", "detail": worst.reason if worst else None}
