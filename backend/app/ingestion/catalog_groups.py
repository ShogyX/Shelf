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

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from ..models import CatalogWork
from .catalog import _media_bucket, _score, _union_find_groups, media_label

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


def _build_groups(rows: list[CatalogWork]) -> list[dict]:
    """Pure: cluster rows → group dicts with representative, tags, raw popularity. No DB."""
    groups: list[dict] = []
    for cluster in _union_find_groups(rows):
        # The card's face should be the most prominent EDITION — popularity first (so the canonical
        # 'One Piece' leads over a niche 'One Piece (Official Colored)'), then data richness. Cover /
        # synopsis fall back to any member's so a popular-but-sparse rep still shows art + blurb.
        rep = max(cluster, key=lambda e: ((e.popularity or 0.0), _score(e, None)))
        cover_url = rep.cover_url or next((m.cover_url for m in cluster if m.cover_url), None)
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
        groups.append({
            "id": rep.id,
            "norm_key": rep.norm_key or "",
            "media_bucket": _media_bucket(rep),
            "title": rep.title,
            "author": rep.author,
            "cover_url": cover_url,
            "synopsis": synopsis,
            "language": rep.language,
            "media_label": media_label(rep),
            "chapters": chapters,
            "popularity": popularity,
            "source_domain": rep.domain,
            "member_count": len(cluster),
            "hooked_work_id": next((m.hooked_work_id for m in cluster if m.hooked_work_id), None),
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


def _should_regroup(db: Session) -> tuple[bool, str]:
    """Skip a rebuild when nothing changed since the last one (catalog churn watermark).
    Returns (changed, mark_json) where mark_json is the value to persist on success."""
    import json
    cur = db.execute(
        select(func.count(CatalogWork.id),
               func.max(func.coalesce(CatalogWork.enriched_at, CatalogWork.updated_at)))
    ).first()
    count, latest = (cur[0] or 0), str(cur[1] or "")
    mark = json.dumps(f"{count}:{latest}")  # JSON-encoded (app_settings.value is a JSON column)
    prev = db.scalar(text("SELECT value FROM app_settings WHERE key = :k"), {"k": _WATERMARK_KEY})
    return prev != mark, mark


def regroup_catalog(db: Session) -> dict:
    """Rebuild the persisted grouping (CatalogGroup/Tag/Category) from the catalog. Idempotent;
    safe to call repeatedly. Returns a summary. CPU + write heavy — call off the event loop."""
    changed, mark = _should_regroup(db)
    if not changed:
        return {"skipped": True, "groups": 0}
    rows = list(db.scalars(select(CatalogWork)).all())
    groups = _build_groups(rows)
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
        "popularity_norm": round(g["popularity_norm"], 6), "source_domain": g["source_domain"],
        "member_count": g["member_count"], "hooked_work_id": g["hooked_work_id"], "updated_at": now,
    } for g in groups]
    _executemany(engine,
        "INSERT INTO catalog_groups (id, norm_key, media_bucket, title, author, cover_url, "
        "synopsis, language, media_label, chapters, popularity_norm, source_domain, member_count, "
        "hooked_work_id, updated_at) VALUES (:id,:norm_key,:media_bucket,:title,:author,:cover_url,"
        ":synopsis,:language,:media_label,:chapters,:popularity_norm,:source_domain,:member_count,"
        ":hooked_work_id,:updated_at)", group_rows)

    member_links = [{"gid": g["id"], "mid": mid} for g in groups for mid in g["member_ids"]]
    _executemany(engine, "UPDATE catalog_works SET group_id = :gid WHERE id = :mid", member_links)

    tag_rows = [{"group_id": g["id"], "kind": k, "slug": s, "label": lab}
                for g in groups for (k, s, lab) in g["tags"]]
    _executemany(engine,
        "INSERT INTO catalog_tags (group_id, kind, slug, label) VALUES (:group_id,:kind,:slug,:label)",
        tag_rows)

    # Category counts: how many groups carry each (kind, slug) per media bucket (genre/theme only).
    from collections import defaultdict
    cat: dict[tuple[str, str, str], dict] = {}
    counts: dict[tuple[str, str, str], int] = defaultdict(int)
    for g in groups:
        for (k, s, lab) in g["tags"]:
            if k not in ("genre", "theme"):
                continue
            key = (k, s, g["media_bucket"])
            counts[key] += 1
            cat.setdefault(key, {"kind": k, "slug": s, "label": lab, "media_bucket": g["media_bucket"]})
    cat_rows = [{**v, "group_count": counts[key]} for key, v in cat.items()]
    _executemany(engine,
        "INSERT INTO catalog_categories (kind, slug, label, media_bucket, group_count) "
        "VALUES (:kind,:slug,:label,:media_bucket,:group_count)", cat_rows)

    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO app_settings (key, value) VALUES (:k, :v) "
                 "ON CONFLICT(key) DO UPDATE SET value = :v"),
            {"k": _WATERMARK_KEY, "v": mark},
        )
    from .. import cache
    cache.clear("catalog-rows:")
    cache.clear("catalog-cat:")
    log.info("catalog regroup: rows=%s groups=%s tags=%s categories=%s",
             len(rows), len(groups), len(tag_rows), len(cat_rows))
    return {"rows": len(rows), "groups": len(groups), "tags": len(tag_rows),
            "categories": len(cat_rows)}


def _executemany(engine, sql: str, params: list[dict]) -> None:
    """Chunked executemany so no single transaction holds the write lock too long."""
    for i in range(0, len(params), _CHUNK):
        with engine.begin() as conn:
            conn.execute(text(sql), params[i:i + _CHUNK])
