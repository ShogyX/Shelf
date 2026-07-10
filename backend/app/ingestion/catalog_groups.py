"""Persisted cross-source grouping for the Index page's discovery rows.

The catalog browse page used to group + dedupe rows on every request, but that union-find is
near-O(n²) and was deliberately capped to a 2000-row recency window to stay fast. The
category-row layout needs to rank/browse the WHOLE catalog by popularity and genre, which that
on-the-fly path can't do without reintroducing the event-loop stalls it was tuned to avoid.

So this module precomputes the grouping in the background and persists it:

  * :class:`CatalogGroup` — one row per logical work (clustered across the sources that carry it),
    with a representative title/cover + a normalized popularity score.
  * :class:`CatalogTag`   — genre/theme/… labels rolled up from the member rows (deduped at the
    work level, so a genre row never lists the same work twice).
  * :class:`CatalogCategory` — which (kind, slug) are populous enough to be a browsable row.

Reads then become cheap indexed ``ORDER BY popularity_norm DESC LIMIT N`` lookups. The group id is
the representative member's catalog id — globally unique and stable across rebuilds, so cached row
responses stay valid while the rep is unchanged. Runs off the event loop (scheduler → ``to_thread``)
with chunked writes so it doesn't fight the crawl for the SQLite write lock.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import func, or_, select, text
from sqlalchemy.orm import Session

from ..models import CatalogWork
from .catalog import MEDIA_LABELS, _media_bucket, _score, _union_find_groups, has_anilist_identity, media_label
from .extract import is_latin_title


def _group_label(cluster, rep) -> str:
    """The group's fine media label. An AUTHORITATIVE metadata label on ANY member (set by
    metadata_sync from e.g. AniList's format) wins over the rep's URL/title heuristic — the enriched
    member isn't necessarily the popularity-chosen rep."""
    for m in cluster:
        ml = (m.extra or {}).get("meta_label")
        if ml in MEDIA_LABELS:
            return ml
    # AniList only carries manga + light novels: if ANY member was AniList-identified, a prose group is a
    # Novel (never a Book), even when the popularity rep is a book-provider member. (Comic groups already
    # resolve to a manga/comic label.)
    if _media_bucket(rep) != "comic" and any(has_anilist_identity(m) for m in cluster):
        return "Novel"
    return media_label(rep)

log = logging.getLogger("shelf.indexer")

_WATERMARK_KEY = "catalog_regroup_watermark_v1"
_MAX_GENRES = 10
_MAX_THEMES = 12
_CHUNK = 500


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _member_tags(row: CatalogWork) -> list[tuple[str, str, str]]:
    """(kind, slug, label) tuples a member row contributes, from its enriched extra."""
    extra = row.extra or {}
    out: list[tuple[str, str, str]] = []
    for kind, key in (("genre", "genres"), ("theme", "themes"),
                      ("demographic", "demographics"), ("format", "format")):
        for t in (extra.get(key) or []):
            slug = (t or {}).get("slug")
            label = (t or {}).get("label")
            if slug and label:
                out.append((kind, slug, label))
    return out


def _build_groups(rows: list[CatalogWork], prior_covers: dict[int, str] | None = None) -> list[dict]:
    """Pure: cluster rows → group dicts with representative, tags, raw popularity. No DB.

    ``prior_covers`` maps a group id → its existing DURABLE (/covers/) cover; such a cover is PRESERVED
    rather than recomputed from members. Comic members carry comix's blocked CDN cover (or none), so
    recomputing every regroup clobbered the AniList cover the backfill had filled in — the cover
    flickered blank on every regroup. Keeping the durable group cover stops that churn."""
    prior_covers = prior_covers or {}
    groups: list[dict] = []
    for cluster in _union_find_groups(rows):
        # The card's face should be the most prominent EDITION — popularity first (so the canonical
        # 'One Piece' leads over a niche 'One Piece (Official Colored)'), then data richness. Cover /
        # synopsis fall back to any member's so a popular-but-sparse rep still shows art + blurb.
        # Deterministic rep (drives DISPLAY fields only — title/cover/synopsis). The catalog is
        # English-canonical, so an English/Latin-script edition ALWAYS wins the display title over a
        # foreign-language one (the Greek "Ἰλιάς" stays a member but "The Iliad" is what's shown); only
        # then popularity, data richness, and -id (a TOTAL-ORDER final tiebreak so ties don't resolve by
        # scan order, which made the shown title/cover flicker between regroups).
        rep = max(cluster, key=lambda e: (
            is_latin_title(e.title) and (e.language or "en") == "en",
            is_latin_title(e.title),
            (e.popularity or 0.0), _score(e, None), -e.id))
        group_id = min(m.id for m in cluster)
        # Prefer a DURABLE cover the backfill already earned for this group, then a localized member
        # cover, then any member cover (raw/remote).
        cover_url = (
            prior_covers.get(group_id)
            or next((m.cover_url for m in cluster if (m.cover_url or "").startswith("/covers/")), None)
            or rep.cover_url or next((m.cover_url for m in cluster if m.cover_url), None)
        )
        synopsis = rep.synopsis or next((m.synopsis for m in cluster if m.synopsis), None)
        # Roll up tags across all members, deduped by (kind, slug); cap genres/themes.
        seen: set[tuple[str, str]] = set()
        tags_by_kind: dict[str, list[tuple[str, str]]] = {}
        for m in cluster:
            for kind, slug, label in _member_tags(m):
                if (kind, slug) in seen:
                    continue
                cap = _MAX_GENRES if kind == "genre" else _MAX_THEMES if kind == "theme" else 6
                bucket = tags_by_kind.setdefault(kind, [])
                if len(bucket) >= cap:
                    continue
                seen.add((kind, slug))
                bucket.append((slug, label))
        tags = [(kind, slug, label)
                for kind, items in tags_by_kind.items() for slug, label in items]
        popularity = max((m.popularity or 0.0) for m in cluster)
        chapters = rep.chapters_advertised or rep.chapters_listed
        # group_id (computed above) = the EARLIEST-discovered member (min id), NOT rep.id: the rep is
        # chosen by popularity+richness and FLIPS when a later enrichment bumps a member or the scan
        # order shifts, which rewrote the persisted group id → the React key changed and the card
        # re-rendered as a "new"/duplicate while the 120s cache still held the old id. min(member id)
        # is invariant under enrichment + scan order, so the id is stable; rep still drives DISPLAY.
        groups.append({
            "id": group_id,
            "norm_key": rep.norm_key or "",
            "media_bucket": _media_bucket(rep),
            "title": rep.title,
            "author": rep.author,
            "cover_url": cover_url,
            "synopsis": synopsis,
            "language": rep.language,
            "media_label": _group_label(cluster, rep),
            "chapters": chapters,
            "is_adult": any(bool(m.is_adult) for m in cluster),  # 18+ if any member is adult
            "popularity": popularity,
            "source_domain": rep.domain,
            "member_count": len(cluster),
            # Deterministic when a cluster has several members hooked to DIFFERENT works (min, not the
            # scan-order-dependent first): the group's hooked work shouldn't flip between regroups.
            "hooked_work_id": min((m.hooked_work_id for m in cluster if m.hooked_work_id),
                                  default=None),
            "member_ids": [m.id for m in cluster],
            "tags": tags,
        })
    return groups


def _normalize_popularity(groups: list[dict]) -> None:
    """Set each group's ``popularity_norm`` (0..1) — the cross-source ranking key — so that
    ABSOLUTE audience dominates and obscure content sits far from the top, while different
    sources stay comparable.

    Why not a plain percentile: raw popularity scales differ wildly per source (AniList user
    counts ~10^5, Open Library reading-log ~10^3, gutendex downloads, comix follows), AND most
    rows are un-enriched (raw 0). A percentile rank spreads those zero/low ties across 0..1, so
    obscure titles surface at the top — exactly what we must avoid.

    Instead:
      1. Calibrate each source to a common scale: ``cal = raw / ref`` where ``ref`` is the source's
         (source_domain, media_bucket) 90th-percentile non-zero popularity — so a "popular for this
         source" title is ~1.0 regardless of the source's raw magnitude (this is what lets a famous
         book rank near a famous manga). Un-enriched / raw-0 rows get ``cal = 0`` → bottom.
      2. Normalize globally by the top calibrated value so the score is LINEAR in audience: a title
         with a tenth of the most-popular's (calibrated) audience scores ~0.1, not ~0.5. The long
         tail of low/zero-audience content collapses toward 0 and can't reach the top rows, while
         the very top still spreads (the single biggest hit = 1.0, the next ones below it).
    See app/ingestion/catalog_enrichment 'Popularity model'."""
    from collections import defaultdict

    def _pct(sorted_vals: list[float], q: float) -> float:
        if not sorted_vals:
            return 0.0
        return sorted_vals[min(len(sorted_vals) - 1, int(len(sorted_vals) * q))]

    parts: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for g in groups:
        parts[(g["source_domain"] or "", g["media_bucket"])].append(g)
    cal_max = 0.0
    for members in parts.values():
        nz = sorted(g["popularity"] for g in members if (g["popularity"] or 0) > 0)
        # p90 anchors "popular for this source"; for a source with too few signals fall back to its
        # max so a single value doesn't define the whole scale.
        ref = (_pct(nz, 0.90) if len(nz) >= 8 else (nz[-1] if nz else 0.0)) or 1.0
        for g in members:
            g["_cal"] = (g["popularity"] / ref) if (g["popularity"] or 0) > 0 else 0.0
            cal_max = max(cal_max, g["_cal"])
    cal_max = cal_max or 1.0
    for g in groups:
        g["popularity_norm"] = round(min(1.0, g.pop("_cal", 0.0) / cal_max), 6)


# Throttle thresholds for the PERIODIC tick (F01): a full rebuild is a DELETE+INSERT of ~600k rows,
# so we don't fire it for a ~0.2% crawl delta every ~10 min. A throttled rebuild waits until enough
# rows changed since the last rebuild OR enough wall-clock elapsed — bounding both write churn and
# grouping staleness. Direct callers (restore/manual/tests) are NOT throttled.
_REGROUP_MIN_DELTA = 500
_REGROUP_MAX_INTERVAL_S = 3 * 3600


def _parse_watermark(raw: str | None) -> dict | None:
    """Parse the stored watermark into {sig, count, ts}. Tolerates the legacy plain-string form
    (returns None → forces one rebuild that upgrades it) and any malformed value."""
    if not raw:
        return None
    import json
    try:
        obj = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(obj, dict) or "sig" not in obj:
        return None
    try:
        ts = datetime.fromisoformat(obj["ts"]) if obj.get("ts") else None
    except (ValueError, TypeError):
        ts = None
    return {"sig": obj["sig"], "count": int(obj.get("count") or 0), "ts": ts}


def _should_regroup(db: Session, throttle: bool = False) -> tuple[bool, str]:
    """Decide whether to rebuild the grouping, returning (changed, mark_json) where mark_json is the
    value to persist on a successful rebuild. The periodic tick passes ``throttle=True`` so a tiny
    crawl delta no longer triggers a full rebuild every ~10 min (F01); direct callers leave it False
    for an immediate rebuild on any change (restore/manual/tests)."""
    import json
    cur = db.execute(
        select(func.count(CatalogWork.id),
               func.max(func.coalesce(CatalogWork.enriched_at, CatalogWork.updated_at)),
               # min/max/sum of ids catch a same-count membership change: deleting one row and adding
               # another (different id) leaves count — and possibly the max-timestamp — unchanged, but
               # always shifts the id sum, so the rebuild is no longer silently skipped.
               func.min(CatalogWork.id), func.max(CatalogWork.id), func.sum(CatalogWork.id))
    ).first()
    count, latest = (cur[0] or 0), str(cur[1] or "")
    sig = f"{count}:{latest}:{cur[2] or 0}:{cur[3] or 0}:{cur[4] or 0}"
    now = datetime.now(UTC)
    mark = json.dumps({"sig": sig, "count": count, "ts": now.isoformat()})

    prev = _parse_watermark(db.scalar(text("SELECT value FROM app_settings WHERE key = :k"),
                                      {"k": _WATERMARK_KEY}))
    if prev is None:
        return True, mark                       # never grouped → build
    if prev["sig"] == sig:
        return False, mark                      # nothing changed → skip
    if not throttle:
        return True, mark                       # any change → immediate rebuild (restore/manual/tests)
    # Throttled periodic tick: defer a small + recent delta.
    delta = abs(count - prev["count"])
    elapsed = (now - prev["ts"]).total_seconds() if prev["ts"] else _REGROUP_MAX_INTERVAL_S
    if delta >= max(_REGROUP_MIN_DELTA, count // 100) or elapsed >= _REGROUP_MAX_INTERVAL_S:
        return True, mark
    return False, mark


def regroup_catalog(db: Session, *, throttle: bool = False) -> dict:
    """Rebuild the persisted grouping (CatalogGroup/Tag/Category) from the catalog. Idempotent;
    safe to call repeatedly. Returns a summary. CPU + write heavy — call off the event loop.
    ``throttle=True`` (periodic tick only) applies the delta/time gate so a tiny crawl change doesn't
    rebuild the whole catalog every tick (F01)."""
    changed, mark = _should_regroup(db, throttle=throttle)
    if not changed:
        return {"skipped": True, "groups": 0}
    # Load + cluster the catalog ONE media bucket at a time (P3): the union-find only ever merges
    # rows of the SAME bucket (_media_bucket gates every union), so comics and prose can be grouped
    # independently — peak memory is the larger bucket, not the whole CatalogWork table (with its
    # heavy synopsis/extra columns). The bucket IS fully materialized (union-find needs every member
    # at once, so it can't be streamed); yield_per only bounds the DB driver's per-round-trip fetch
    # buffer so the raw rows aren't all buffered before ORM-ization. The (much smaller) group dicts
    # accumulate across buckets so popularity still normalizes globally. NB: the non-comic filter must
    # include NULL media_kind (which _media_bucket treats as 'text'), or those rows would silently
    # drop out of BOTH buckets.
    groups: list[dict] = []
    row_count = 0
    # Durable covers (/covers/) already earned for each group (e.g. AniList comic-cover backfill) —
    # keyed by the stable group id (= min member id), which is exactly CatalogGroup.id. Preserve them
    # so a rebuild from members (whose comix covers are blocked/none) doesn't clobber them every pass.
    from ..models import CatalogGroup as _CG
    prior_covers: dict[int, str] = {
        gid: url for gid, url in db.execute(
            select(_CG.id, _CG.cover_url).where(_CG.cover_url.like("/covers/%"))
        ).all() if url
    }
    for bucket_filter in (
        CatalogWork.media_kind == "comic",
        or_(CatalogWork.media_kind != "comic", CatalogWork.media_kind.is_(None)),
    ):
        # ORDER BY id so each cluster's member list is in a deterministic order — the rep is already
        # a total order (popularity, richness, -id), but the cover/synopsis `next(...)` fallbacks and
        # the hooked-work pick below would otherwise track SQLite's physical scan order and flip
        # between regroups.
        rows = list(db.scalars(
            select(CatalogWork).where(bucket_filter)
            .order_by(CatalogWork.id).execution_options(yield_per=2000)
        ).all())
        row_count += len(rows)
        groups.extend(_build_groups(rows, prior_covers))
        del rows  # release this bucket before loading the next
    _normalize_popularity(groups)

    now = _utcnow()
    from ..db import engine
    # Rebuild tables (derived cache). Clear first, then bulk-insert in chunks so a single huge
    # transaction never holds the write lock against the crawl.
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM catalog_tags"))
        conn.execute(text("DELETE FROM catalog_categories"))
        conn.execute(text("DELETE FROM catalog_groups"))
        conn.execute(text("UPDATE catalog_works SET group_id = NULL"))

    group_rows = [{
        "id": g["id"], "norm_key": g["norm_key"], "media_bucket": g["media_bucket"],
        "title": g["title"][:512], "author": (g["author"] or None),
        "cover_url": g["cover_url"], "synopsis": g["synopsis"], "language": g["language"],
        "media_label": g["media_label"], "chapters": g["chapters"],
        "is_adult": 1 if g.get("is_adult") else 0,
        "popularity_norm": round(g["popularity_norm"], 6), "source_domain": g["source_domain"],
        "member_count": g["member_count"], "hooked_work_id": g["hooked_work_id"], "updated_at": now,
    } for g in groups]
    _executemany(engine,
        "INSERT INTO catalog_groups (id, norm_key, media_bucket, title, author, cover_url, "
        "synopsis, language, media_label, chapters, is_adult, popularity_norm, source_domain, "
        "member_count, hooked_work_id, updated_at) VALUES (:id,:norm_key,:media_bucket,:title,:author,"
        ":cover_url,:synopsis,:language,:media_label,:chapters,:is_adult,:popularity_norm,"
        ":source_domain,:member_count,:hooked_work_id,:updated_at)", group_rows)

    member_links = [{"gid": g["id"], "mid": mid} for g in groups for mid in g["member_ids"]]
    _executemany(engine, "UPDATE catalog_works SET group_id = :gid WHERE id = :mid", member_links)

    tag_rows = [{"group_id": g["id"], "kind": k, "slug": s, "label": lab}
                for g in groups for (k, s, lab) in g["tags"]]
    _executemany(engine,
        "INSERT INTO catalog_tags (group_id, kind, slug, label) VALUES (:group_id,:kind,:slug,:label)",
        tag_rows)

    # Category counts: how many groups carry each (kind, slug) per media_label (genre/theme only),
    # so a genre lane appears under each category it's populous in (Manga vs Manhua vs Webtoon …).
    from collections import defaultdict
    cat: dict[tuple[str, str, str], dict] = {}
    counts: dict[tuple[str, str, str], int] = defaultdict(int)
    for g in groups:
        for (k, s, lab) in g["tags"]:
            if k not in ("genre", "theme"):
                continue
            key = (k, s, g["media_label"])
            counts[key] += 1
            cat.setdefault(key, {"kind": k, "slug": s, "label": lab,
                                 "media_label": g["media_label"], "media_bucket": g["media_bucket"]})
    cat_rows = [{**v, "group_count": counts[key]} for key, v in cat.items()]
    _executemany(engine,
        "INSERT INTO catalog_categories (kind, slug, label, media_bucket, media_label, group_count) "
        "VALUES (:kind,:slug,:label,:media_bucket,:media_label,:group_count)", cat_rows)

    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO app_settings (key, value) VALUES (:k, :v) "
                 "ON CONFLICT(key) DO UPDATE SET value = :v"),
            {"k": _WATERMARK_KEY, "v": mark},
        )
    from .. import cache
    # Clear the WHOLE catalog-* prefix (rows, cat, base, facets, stats) — a regroup rebuilds every
    # one of them, so clearing only rows/cat left the /catalog list + /catalog/facets + stats serving
    # the pre-regroup grouping for their 15s TTL right after the rebuild whose point is freshness.
    # force: the ONE clear for the whole rebuild — swallowed by the 20s throttle it would pin
    # a mid-rebuild partial snapshot in the caches for the full 30min TTL.
    cache.clear_catalog(force=True)
    log.info("catalog regroup: rows=%s groups=%s tags=%s categories=%s",
             row_count, len(groups), len(tag_rows), len(cat_rows))
    return {"rows": row_count, "groups": len(groups), "tags": len(tag_rows),
            "categories": len(cat_rows)}


def _executemany(engine, sql: str, params: list[dict]) -> None:
    """Chunked executemany so no single transaction holds the write lock too long. Each chunk retries
    on a transient SQLite 'database is locked' — under the continuous crawl, a write burst / WAL
    checkpoint can hold the single writer past busy_timeout, and a regroup shouldn't be lost (and spam
    a 'job failed' alert) over momentary contention. Each chunk is its own transaction, so re-running
    a locked chunk is safe + idempotent for the upsert/update SQL used here."""
    import time as _t

    from sqlalchemy.exc import OperationalError
    for i in range(0, len(params), _CHUNK):
        chunk = params[i:i + _CHUNK]
        for attempt in range(5):
            try:
                with engine.begin() as conn:
                    conn.execute(text(sql), chunk)
                break
            except OperationalError as exc:
                if "locked" not in str(exc).lower() or attempt == 4:
                    raise
                _t.sleep(0.3 * (attempt + 1))
