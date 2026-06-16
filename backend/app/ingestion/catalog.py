"""The discovered-works catalog — a searchable 'card catalog' built while indexing.

The smart indexer feeds pages in via :func:`upsert_from_page` (only literature pages
become catalog entries). The API reads them back grouped + deduped across sites via
:func:`group_rows` so the same title found on several sources is one card with a
source picker. Hooking a chosen source is handled in :mod:`.diagnose`.
"""
from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from urllib.parse import urlparse

from sqlalchemy import exists, func, or_, select
from sqlalchemy.orm import Session

from ..models import CatalogWork, IndexSite, Work
from .base import registry
from .extract import (
    _EDITION_MARKERS,
    _author_norm,
    _is_gutenberg_book,
    _is_site_name_title,
    classify_page,
    detect_media_kind,
    is_junk_url,
    is_listing_url,
    norm_title,
    og_title,
    page_metadata,
    split_byline,
    work_title_from,
    work_url_for,
)

log = logging.getLogger("shelf.indexer")

# Page kinds that represent (part of) a literary work worth cataloging.
_LIT_KINDS = ("work", "toc", "chapter")


def _utcnow() -> datetime:
    return datetime.now(UTC)


# Domains with a purpose-built adapter (better metadata + chapterization than the generic
# adaptive crawler); everything else uses generic_feed.
_DOMAIN_ADAPTERS = {
    "standardebooks.org": "standardebooks",
    "gutenberg.org": "gutenberg",
    "j-novel.club": "jnovel",
    "comix.to": "comix",
}


def _source_key_for(entry: CatalogWork) -> str:
    # Match on host suffix (not substring) so look-alikes like notgutenberg.org or
    # evil.com/?ref=gutenberg.org can't misroute to a dedicated adapter.
    dom = (entry.domain or "").lower()
    if dom.startswith("www."):
        dom = dom[4:]
    for needle, key in _DOMAIN_ADAPTERS.items():
        if dom == needle or dom.endswith("." + needle):
            return key
    return "generic_feed"


# comix.to is an SPA that sets NO og:image, so generic cover extraction finds nothing — but the
# title page renders the poster as a static.comix.to image. Grab the first one (the work's own
# cover, at the top of the page) and drop the "@280" thumbnail-size suffix for the full-res cover.
_COMIX_POSTER_RE = re.compile(r'https://static\.comix\.to/[^\s"\'<>]+\.(?:webp|jpe?g|png)', re.I)


def _comix_cover(html: str) -> str | None:
    m = _COMIX_POSTER_RE.search(html or "")
    if not m:
        return None
    return re.sub(r"@\d+(?=\.\w+$)", "", m.group(0))


def upsert_from_page(db: Session, site: IndexSite, html: str, url: str, *,
                     meta: dict | None = None, title: str | None = None) -> CatalogWork | None:
    """If a fetched page is literature, create/update its catalog entry and return it.

    A chapter page is attributed to its *parent work* (so many chapters collapse to one
    catalog entry). Returns None for non-literature pages (browse/account/legal/home).
    Richer fields (cover/synopsis/advertised count) are only *upgraded*, never blanked,
    so a later landing-page fetch enriches an entry first seen via a chapter page.

    ``meta``/``title`` let a caller supply already-extracted values — used by the catalog
    *reconciler* to rebuild a lost entry from a stored page (whose sanitized HTML has no
    ``<head>``) without re-fetching; omitted on the live crawl, where they're read from ``html``.
    """
    pc = classify_page(html, url, meta=meta, title=title)
    if pc.kind not in _LIT_KINDS:
        return None
    work_url = pc.work_url or url
    # Operator-removed (blocked) content must not be re-catalogued by a later crawl.
    from . import blocklist
    if blocklist.is_blocked(db, work_url) or blocklist.is_blocked(db, url):
        return None
    meta = meta if meta is not None else page_metadata(html, url)
    title = (pc.title or work_title_from(title or og_title(html)) or work_url)[:512]
    # Project Gutenberg puts the byline in the page title with no separate author field —
    # "Moby Dick; Or, The Whale by Herman Melville". Split it so the card shows a clean
    # title + author. Scoped to Gutenberg, where "Title by Author" is a reliable convention
    # (splitting blindly would mangle real titles like "Surrounded by Idiots").
    byline_author = None
    if not meta.get("author") and _is_gutenberg_book(url):
        work_title, byline_author = split_byline(title)
        if byline_author:
            title = work_title[:512]

    entry = db.scalar(
        select(CatalogWork).where(
            CatalogWork.site_id == site.id, CatalogWork.work_url == work_url
        )
    )
    created = entry is None
    if entry is None:
        entry = CatalogWork(site_id=site.id, work_url=work_url, domain=site.domain, title=title)
        db.add(entry)

    # A real landing/TOC page is the authoritative title; don't let a later chapter
    # page (whose og:title is "… Chapter N") clobber a good work title.
    if pc.kind in ("work", "toc") or created or not entry.norm_key:
        entry.title = title
        entry.norm_key = norm_title(title)
    if pc.kind in ("work", "toc"):
        entry.kind = "work"
    elif created:
        entry.kind = "work"  # attribute chapter→parent work

    # Upgrade-only enrichment.
    if meta.get("author") and not entry.author:
        entry.author = meta["author"]
    elif byline_author and not entry.author:
        entry.author = byline_author
    cover = meta.get("cover_url")
    if not cover and "comix.to" in (site.domain or url):
        cover = _comix_cover(html)  # SPA with no og:image — pull the poster from the DOM
    if cover and not entry.cover_url:
        entry.cover_url = cover
    if meta.get("description") and (
        not entry.synopsis or len(meta["description"]) > len(entry.synopsis)
    ):
        entry.synopsis = meta["description"]
    if meta.get("language") and not entry.language:
        entry.language = meta["language"]
    # Detect comic/manga so the library + reader treat it as images, not prose. Upgrade
    # text→comic when any page of the work shows a comic signal (never downgrade).
    if entry.media_kind != "comic":
        mk = detect_media_kind(
            url, og_type=meta.get("type"), site_name=meta.get("site_name"), title=title
        )
        if mk == "comic":
            entry.media_kind = "comic"
    if pc.advertised:
        entry.chapters_advertised = max(entry.chapters_advertised or 0, pc.advertised)
    if pc.listed:
        entry.chapters_listed = max(entry.chapters_listed or 0, pc.listed)
    entry.updated_at = _utcnow()
    db.commit()
    db.refresh(entry)
    return entry


# Catalog reconciliation — a one-time backlog heal so titles that were ALREADY crawled (their
# IndexedPage is still stored) but whose CatalogWork went missing (e.g. a wipe) reappear in the
# Index, WITHOUT re-fetching them from the network. A CatalogWork is normally built only at the
# instant a page transitions pending→fetched; a page that's already 'fetched' is never re-fetched
# (frontier dedup), so a lost entry would otherwise stay lost. This sweeps fetched pages by id with
# a persisted cursor, rebuilding any missing entry from the page's STORED content, then idles.
_RECONCILE_CURSOR_KEY = "catalog_reconcile_cursor"


def reconcile_catalog_tick(db: Session, *, limit: int = 300) -> dict:
    """Rebuild missing catalog entries for already-fetched index pages (no network). Bounded per
    call and cursor-tracked over ``IndexedPage.id`` so it sweeps the fetched backlog exactly once
    and then does nothing. Only authoritative landing/TOC pages seed an entry, and pages already
    represented in the catalog are skipped — so this never churns existing rows."""
    from ..models import AppSetting, IndexedPage
    from . import comix_catalog
    from .extract import work_url_for

    row = db.get(AppSetting, _RECONCILE_CURSOR_KEY)
    cursor = int(row.value.get("page_id", 0)) if (row and isinstance(row.value, dict)) else 0
    pages = db.scalars(
        select(IndexedPage)
        .where(
            IndexedPage.status == "fetched",
            IndexedPage.id > cursor,
            IndexedPage.html.is_not(None),
        )
        .order_by(IndexedPage.id)
        .limit(max(1, limit))
    ).all()
    if not pages:
        return {"done": True, "scanned": 0, "rebuilt": 0, "cursor": cursor}

    site_cache: dict[int, IndexSite | None] = {}
    rebuilt = scanned = 0
    last_id = cursor
    for p in pages:
        last_id = p.id
        scanned += 1
        if p.site_id not in site_cache:
            site_cache[p.site_id] = db.get(IndexSite, p.site_id)
        site = site_cache[p.site_id]
        # API-catalog sites (comix.to) rebuild via their own periodic API refresh, not HTML pages.
        if site is None or comix_catalog.is_api_catalog_site(site):
            continue
        # Reconstruct the metadata that was extracted from the full page at fetch time (the stored
        # HTML is the sanitized BODY — no <head> — so og: tags would otherwise be unreadable).
        meta = {
            "description": p.description, "author": p.author, "cover_url": p.cover_url,
            "site_name": p.site_name, "type": p.page_type, "language": None,
        }
        try:
            pc = classify_page(p.html, p.url, meta=meta, title=p.title or "")
            # Only landing/TOC pages are authoritative catalog seeds; chapter pages get a poor
            # parent title and are covered by their work's own page (also being swept).
            if pc.kind not in ("work", "toc"):
                continue
            work_url = pc.work_url or p.url
            if db.scalar(
                select(CatalogWork.id).where(
                    CatalogWork.site_id == p.site_id,
                    CatalogWork.work_url.in_({work_url, work_url_for(p.url), p.url}),
                )
            ):
                continue  # already in the catalog → don't rebuild/churn
            if upsert_from_page(db, site, p.html, p.url, meta=meta, title=p.title or "") is not None:
                rebuilt += 1
        except Exception:  # noqa: BLE001 — one bad page must not abort the sweep
            db.rollback()
            log.exception("catalog reconcile failed for page %s", p.id)

    if row is None:
        db.add(AppSetting(key=_RECONCILE_CURSOR_KEY, value={"page_id": last_id}))
    else:
        row.value = {"page_id": last_id}
    db.commit()
    if rebuilt:
        log.info("catalog reconcile: scanned=%s rebuilt=%s cursor->%s", scanned, rebuilt, last_id)
    return {"done": len(pages) < max(1, limit), "scanned": scanned,
            "rebuilt": rebuilt, "cursor": last_id}


def upsert_external(db: Session, integration, ext) -> CatalogWork | None:
    """Create/update a catalog entry from an integration's ExternalWork. Deduped by
    (provider, provider_ref, integration). Copies the metadata the integration pulled.
    Returns None when the work is on the operator blocklist (so a sync can't re-add it)."""
    from . import blocklist
    if ext.url and blocklist.is_blocked(db, ext.url):
        return None
    ref = ext.ref or f"title:{norm_title(ext.title)}"
    entry = db.scalar(
        select(CatalogWork).where(
            CatalogWork.provider == ext.provider,
            CatalogWork.provider_ref == ref,
            CatalogWork.integration_id == integration.id,
        )
    )
    if entry is None:
        entry = CatalogWork(
            provider=ext.provider, provider_ref=ref, integration_id=integration.id,
            domain=integration.name or integration.kind,
            work_url=ext.url or f"{ext.provider}:{ref}", title=ext.title[:512],
        )
        db.add(entry)
    entry.title = ext.title[:512]
    entry.norm_key = norm_title(ext.title)
    if ext.author:
        entry.author = ext.author[:255]
    if ext.cover_url:
        entry.cover_url = ext.cover_url
    if ext.overview and (not entry.synopsis or len(ext.overview) > len(entry.synopsis)):
        entry.synopsis = ext.overview
    entry.media_kind = ext.media_kind
    entry.kind = "work"
    if ext.url:
        entry.work_url = ext.url
    entry.extra = {
        **(ext.extra or {}),
        "in_library": ext.in_library,
        "downloaded": ext.downloaded,
        "integration_kind": integration.kind,
        "root_folder": integration.root_folder,
        "year": ext.year,
    }
    entry.updated_at = _utcnow()
    db.commit()
    db.refresh(entry)
    return entry


def find_rows(
    db: Session,
    *,
    q: str | None = None,
    site_id: int | None = None,
    hooked: bool | None = None,
    limit: int = 600,
) -> list[CatalogWork]:
    """Fetch candidate catalog rows (pre-grouping). ``q`` is a case-insensitive LIKE
    over title/author/synopsis/normalized-key."""
    sel = select(CatalogWork)
    if site_id is not None:
        sel = sel.where(CatalogWork.site_id == site_id)
    if hooked is True:
        sel = sel.where(CatalogWork.hooked_work_id.is_not(None))
    elif hooked is False:
        sel = sel.where(CatalogWork.hooked_work_id.is_(None))
    if q:
        like = f"%{q.strip()}%"
        nk = f"%{norm_title(q)}%"
        sel = sel.where(
            or_(
                CatalogWork.title.ilike(like),
                CatalogWork.author.ilike(like),
                CatalogWork.synopsis.ilike(like),
                CatalogWork.norm_key.like(nk),
            )
        )
    # Order by popularity first so the browse/"popular" candidate pool actually contains the
    # most-popular works (not just the most-recently-updated); recency breaks ties. Search results
    # are re-ranked by relevance in group_rows, so popularity-first here only widens recall.
    sel = sel.order_by(CatalogWork.popularity.desc(), CatalogWork.updated_at.desc()).limit(limit)
    return list(db.scalars(sel).all())


def _score(entry: CatalogWork, q: str | None) -> tuple:
    """Rank an entry as the representative of its group / for search ordering."""
    chapters = entry.chapters_advertised or entry.chapters_listed or 0
    title_hit = 0
    if q:
        ql = q.lower()
        if ql in (entry.title or "").lower():
            title_hit = 2
        elif ql in (entry.author or "").lower():
            title_hit = 1
    return (title_hit, bool(entry.synopsis), bool(entry.cover_url), chapters)


def _media_bucket(e: CatalogWork) -> str:
    """Coarse media class for grouping: comics never merge with prose."""
    return "comic" if (e.media_kind or "text") == "comic" else "text"


def _union_find_groups(rows: list[CatalogWork]) -> list[list[CatalogWork]]:
    """Cluster rows by strong title+author matching (not just exact normalized title),
    so the same work from web crawl + Readarr + Kapowarr lands in one group.

    Two entries only merge when they're the SAME media class (a novel and its manga
    adaptation share a title but are different works) AND their titles+authors match — so
    e.g. 'My Next Life as a Villainess' the light novel and the manga stay distinct cards."""
    n = len(rows)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        parent[find(i)] = find(j)

    keys = [r.norm_key or norm_title(r.title) for r in rows]
    media = [_media_bucket(r) for r in rows]
    authors = [r.author for r in rows]
    toks = [frozenset(k.split()) for k in keys]  # precompute once (was recomputed per pair)
    # Precompute normalized author token-sets ONCE per row (was recomputed inside authors_compatible
    # on BOTH authors for every candidate pair — ~15M _author_norm calls / regroup). An empty set
    # means "author unknown" → never blocks a title match (mirrors authors_compatible's na/nb guard).
    atoks = [frozenset(_author_norm(a).split()) for a in authors]

    # Identity buckets FIRST (K1 / 14A): rows carrying the SAME non-null identity_key
    # ("anilist:123", "isbn:…", a provider_ref) are the same work regardless of title — this merges
    # cross-source/cross-language variants (romaji vs English, native-only, subtitle-on-one-source)
    # that title normalization can't reconcile. Still scoped by media class so a novel and its manga
    # adaptation (different identity_keys anyway) never collapse.
    by_identity: dict[tuple[str, str], int] = {}
    for i, r in enumerate(rows):
        ident = getattr(r, "identity_key", None)
        if not ident:
            continue
        ik = (ident, media[i])
        if ik in by_identity:
            union(i, by_identity[ik])
        else:
            by_identity[ik] = i

    # Exact-key buckets first (cheap): bucket by (normalized title, media class) so a novel and
    # its comic adaptation don't collapse into one card just because the title strings match.
    by_key: dict[tuple[str, str], int] = {}
    for i, k in enumerate(keys):
        if not k:
            continue   # an EMPTY normalized key is not an identity — never union on it, or every
                       # empty-key row in a media bucket would collapse into one bogus mega-group
                       # (the catastrophic over-merge; with E1, CJK titles no longer hit this).
        bk = (k, media[i])
        if bk in by_key:
            union(i, by_key[bk])
        else:
            by_key[bk] = i

    # Fuzzy merge — token-blocked instead of O(n²). A fuzzy match needs Jaccard ≥ 0.8, which is
    # impossible without a shared token, so we only compare candidates that share one (inverted
    # index). Block each row on its RAREST token (min posting length), NOT by skipping large buckets:
    # the old "skip any token with >200 rows" dropped 2-token titles whose BOTH tokens are common in
    # a comic-heavy catalog ("Dragon Ball", "Solo Leveling") — neither token's bucket was compared,
    # so the pair never merged. The rarest token is the most selective, so each row is compared only
    # within its smallest bucket (keeps the ~O(n) win) and popular short titles are recovered.
    from collections import defaultdict
    postings: dict[str, list[int]] = defaultdict(list)
    for i, ts in enumerate(toks):
        if len(ts) >= 2:  # one-word titles never fuzzy-merge (titles_match requires ≥2 tokens)
            for tok in ts:
                postings[tok].append(i)
    rare_buckets: dict[str, list[int]] = defaultdict(list)
    for i, ts in enumerate(toks):
        if len(ts) < 2:
            continue
        # Block on the 2 RAREST tokens, excluding edition-qualifier words: an edition adds a rare
        # qualifier ("One Piece Colored") that would otherwise become the sole rarest token and land
        # the edition in a bucket the base title never shares. Keying on the rarest CORE tokens keeps
        # editions together AND recovers common-short-title pairs. Two keys (not one) so a base title
        # and a one-extra-word variant still share a bucket.
        core = [t for t in ts if t not in _EDITION_MARKERS] or list(ts)
        for tok in sorted(core, key=lambda t: len(postings[t]))[:2]:
            rare_buckets[tok].append(i)
    for idxs in rare_buckets.values():
        for a in range(len(idxs)):
            i = idxs[a]
            ti = toks[i]
            for b in range(a + 1, len(idxs)):
                j = idxs[b]
                if media[i] != media[j] or find(i) == find(j):
                    continue
                tj = toks[j]
                inter = len(ti & tj)
                # authors_compatible(i, j) inlined on precomputed token-sets: incompatible only when
                # BOTH authors are known and share no token.
                ai, aj = atoks[i], atoks[j]
                if inter == 0 or (ai and aj and not (ai & aj)):
                    continue
                # Mirror titles_match on the precomputed token sets (exact-equal already merged):
                # same work in another EDITION (identical core once edition qualifiers are removed),
                # else a STRONG fuzzy overlap (≥ 0.8). 0.8 (not 0.6) keeps a distinct spin-off word
                # ('One Piece Party') from collapsing into the base work.
                core_i, core_j = ti - _EDITION_MARKERS, tj - _EDITION_MARKERS
                if (core_i and core_i == core_j) or inter / len(ti | tj) >= 0.8:
                    union(i, j)

    clusters: dict[int, list[CatalogWork]] = {}
    for i, r in enumerate(rows):
        clusters.setdefault(find(i), []).append(r)
    return list(clusters.values())


# Book metadata providers are LISTINGS only — they describe books but you can't read or fetch a
# book FROM them. The UI must not offer hook/grab on these; acquisition goes through the pipeline
# (Acquire → Prowlarr/SABnzbd) or a download manager instead.
LISTING_PROVIDERS = frozenset({"googlebooks", "openlibrary", "hardcover"})


def _source_kind(e: CatalogWork) -> str:
    return "online" if e.provider == "web_index" else e.provider


_MANHUA_RE = re.compile(r"manhua", re.I)
_MANGA_RE = re.compile(r"\bmanga\b", re.I)
_WEBTOON_RE = re.compile(r"manhwa|webtoon|\btoon", re.I)  # manhwa (Korean) reads as a webtoon

# The fine per-title media LABELS, in display order. `media_label()` returns exactly one of these;
# they're shown as the badge on each title/source so a user can still tell a Manga from a Webtoon.
# (Distinct from `_media_bucket`'s coarse comic/text split, which only governs cross-source grouping.)
MEDIA_LABELS = ["Manga", "Manhua", "Webtoon", "Comic", "Novel", "Book"]

# The coarse media CATEGORIES the Index organizes sections / filters / per-user-toggles / permissions
# by, in display order. The four comic labels collapse into one "Manga & Comics" category (kept on
# the same level as Novel and Book) so the Index isn't flooded with near-duplicate comic lanes.
COMICS_CATEGORY = "Manga & Comics"
_COMIC_LABELS = ("Manga", "Manhua", "Webtoon", "Comic")
MEDIA_CATEGORIES = [COMICS_CATEGORY, "Novel", "Book"]

_DEFAULT_CATEGORIES_KEY = "default_user_categories"  # AppSetting key for the normal-user default


def media_category(label: str) -> str:
    """The Index category a fine media label rolls up into (the four comic labels → one)."""
    return COMICS_CATEGORY if label in _COMIC_LABELS else label


def category_labels(category: str) -> list[str]:
    """The fine media labels that belong to a category — for filtering groups/categories by it."""
    return list(_COMIC_LABELS) if category == COMICS_CATEGORY else [category]


def _clean_categories(cats) -> list[str]:
    """Keep only valid CATEGORY labels, in canonical order, deduped. Legacy fine comic labels
    (Manga/Manhua/Webtoon/Comic) are folded into 'Manga & Comics' so old saved values still grant
    access after the merge."""
    s = {media_category(c) for c in (cats or [])}
    s &= set(MEDIA_CATEGORIES)
    return [c for c in MEDIA_CATEGORIES if c in s]


def get_default_categories(db: Session) -> list[str] | None:
    """The admin-set default category cap for normal users. ``None`` = no cap (all categories)."""
    from ..models import AppSetting
    row = db.get(AppSetting, _DEFAULT_CATEGORIES_KEY)
    val = row.value if row else None
    return _clean_categories(val) if isinstance(val, list) else None


def set_default_categories(db: Session, cats: list[str] | None) -> list[str] | None:
    """Set (or clear, with ``None``) the normal-user default category cap. Returns the new value."""
    from ..models import AppSetting
    row = db.get(AppSetting, _DEFAULT_CATEGORIES_KEY)
    if cats is None:
        if row is not None:
            db.delete(row)
        db.commit()
        return None
    clean = _clean_categories(cats)
    if row is None:
        db.add(AppSetting(key=_DEFAULT_CATEGORIES_KEY, value=clean))
    else:
        row.value = clean
    db.commit()
    return clean


def effective_categories(db: Session, user) -> list[str]:
    """Which media categories ``user`` may view on the Index. Admins are unrestricted; a normal
    user is capped by their own ``allowed_categories``, else the global default, else all."""
    if user is None or getattr(user, "role", None) == "admin":
        return list(MEDIA_CATEGORIES)
    allowed = user.allowed_categories
    if allowed is None:
        allowed = get_default_categories(db)
    if allowed is None:
        return list(MEDIA_CATEGORIES)
    return _clean_categories(allowed)


# ----------------------------------------------------------------- adult (18+)
# Explicit-adult genre markers (operator chose "explicit only" — NOT Mature/Ecchi, which are often
# just dark or suggestive). Matched against a row's enriched genres (slug or label, case-insensitive).
# A provider adult flag (AniList isAdult / Google Books MATURE) is folded in by the enricher as
# ``extra['adult'] = True``.
_ADULT_GENRE_SLUGS = frozenset({
    "hentai", "adult", "smut", "erotica", "erotic", "pornographic", "porn", "ecchi-adult",
})
_ADULT_ALLOWED_KEY = "adult_allowed_categories"  # AppSetting: categories where 18+ MAY be shown


def is_adult_genre(slug_or_label: str | None) -> bool:
    return (slug_or_label or "").strip().lower() in _ADULT_GENRE_SLUGS


def taxonomy_is_adult(extra: dict | None) -> bool:
    """True when a row's enriched taxonomy marks it 18+: an explicit-adult genre, or a provider
    adult flag (``extra['adult']``)."""
    extra = extra or {}
    if extra.get("adult"):
        return True
    return any(is_adult_genre(g.get("slug")) or is_adult_genre(g.get("label"))
               for g in (extra.get("genres") or []))


def get_adult_allowed(db: Session) -> list[str]:
    """Categories where the admin permits 18+ content at all (the instance gate). Enabled for every
    category by DEFAULT — an admin narrows it (or sets it empty to disable 18+ entirely). The
    distinction is no-row (never configured → all) vs an explicit list (even ``[]`` → exactly that)."""
    from ..models import AppSetting
    row = db.get(AppSetting, _ADULT_ALLOWED_KEY)
    if row is None:
        return list(MEDIA_CATEGORIES)  # default: 18+ permitted everywhere until an admin narrows it
    return _clean_categories(row.value) if isinstance(row.value, list) else list(MEDIA_CATEGORIES)


def set_adult_allowed(db: Session, cats) -> list[str]:
    """Set which categories the instance permits 18+ content in (admin gate). Returns the new value."""
    from ..models import AppSetting
    clean = _clean_categories(cats or [])
    row = db.get(AppSetting, _ADULT_ALLOWED_KEY)
    if row is None:
        db.add(AppSetting(key=_ADULT_ALLOWED_KEY, value=clean))
    else:
        row.value = clean
    db.commit()
    return clean


def effective_adult_categories(db: Session, user) -> list[str]:
    """Categories where THIS viewer sees 18+ content = the admin gate ∩ the user's own preference.
    Enabled by DEFAULT: a user who has never changed it (``User.adult_categories is None``) inherits
    the whole gate; an explicit list (even ``[]`` to turn it all off) is honoured exactly. Bounded by
    the admin gate either way — and it applies to admins too (visibility is a preference, not a
    permission)."""
    allowed = set(get_adult_allowed(db))
    if not allowed:
        return []
    opt = getattr(user, "adult_categories", None) if user is not None else None
    if not isinstance(opt, list):
        return [c for c in MEDIA_CATEGORIES if c in allowed]  # never set → inherit the full gate
    chosen = set(_clean_categories(opt))
    return [c for c in MEDIA_CATEGORIES if c in allowed and c in chosen]


def media_label(e: CatalogWork) -> str:
    """A human label for what a source actually is — so the user knows whether they're hooking a
    Novel, a Book, Manga, Manhua, a Webtoon, or a Comic (not just 'text'/'comic'). One of
    ``MEDIA_LABELS``; these collapse to a ``media_category`` for the Index sections."""
    dom = (e.domain or "").lower()
    hay = f"{dom} {(e.work_url or '').lower()} {(e.title or '').lower()}"
    if _media_bucket(e) == "comic":
        # An API-ingested comix.to entry carries its exact type (manga/manhua/manhwa) — use it so
        # e.g. One Piece lands under Manga even though its URL has no '/manga/' token.
        ctype = ((e.extra or {}).get("comix_type") or "").lower()
        if ctype == "manhua":
            return "Manhua"
        if ctype == "manga":
            return "Manga"
        if ctype == "manhwa":
            return "Webtoon"
        if _MANHUA_RE.search(hay):
            return "Manhua"
        if _WEBTOON_RE.search(hay):
            return "Webtoon"
        if _MANGA_RE.search(hay):
            return "Manga"
        return "Comic"
    if ((dom == "gutenberg.org" or dom.endswith(".gutenberg.org"))
            or (dom == "standardebooks.org" or dom.endswith(".standardebooks.org"))
            or e.provider in ("readarr", "googlebooks", "openlibrary", "hardcover")):
        return "Book"
    # "Novel" is reserved for web / light / Asian-style novels (j-novel, ranobedb, novelupdates,
    # and crawled web-novel sites). Everything else prose defaults to Book.
    if e.provider in ("ranobedb", "novelupdates", "jnovel") or "j-novel" in dom or "ranobe" in dom:
        return "Novel"
    if e.provider == "web_index":
        return "Novel"
    return "Book"


def _source_dict(e: CatalogWork) -> dict:
    """One catalog row → the selectable-source dict the UI hooks/grabs from."""
    return {
        "catalog_id": e.id,
        # Each source's OWN title/author/cover/synopsis (the "sub-title" a given
        # site matched) so the user can compare and pick the right one to hook.
        "title": e.title,
        "author": e.author,
        "cover_url": e.cover_url,
        "synopsis": e.synopsis,
        "site_id": e.site_id,
        "domain": e.domain,
        "work_url": e.work_url,
        "provider": e.provider,
        "kind": _source_kind(e),
        # What this source actually is, so the user knows what they're hooking.
        "media_kind": e.media_kind,
        "media_label": media_label(e),
        "integration_id": e.integration_id,
        "chapters_advertised": e.chapters_advertised,
        "chapters_listed": e.chapters_listed,
        "health": e.health,
        "health_detail": e.health_detail,
        "hooked_work_id": e.hooked_work_id,
        "grab_status": (e.extra or {}).get("grab_status"),
        # A listing-only metadata source can't be hooked/grabbed directly — the UI offers Acquire
        # (pipeline) instead.
        "listing_only": e.provider in LISTING_PROVIDERS,
    }


def _group_series(entries: list[CatalogWork]) -> str | None:
    """The series name for a group, if any member is a known multi-volume-series member (stored on
    extra['series'] by the Hardcover seed/resolve). Drives the UI's 'View Series' affordance — only
    shown for titles that are actually part of a series."""
    for e in entries:
        s = (e.extra or {}).get("series")
        if isinstance(s, str) and s.strip():
            return s.strip()
    return None


def dedupe_sources(entries: list[CatalogWork]) -> list[CatalogWork]:
    """Collapse only TRUE duplicates — the same work re-discovered under two URLs on one site
    (same domain + same normalized title) — keeping the richest (callers pass entries pre-sorted by
    score). Genuinely distinct EDITIONS on the same site (e.g. 'One Piece' vs 'One Piece (Official
    Colored)' — different titles, different content) stay separate selectable sources, so the card
    surfaces every edition and the user can hook the one they want. Different domains always stay
    distinct."""
    seen: set[tuple[str, str]] = set()
    deduped: list[CatalogWork] = []
    for e in entries:
        dom = (e.domain or "").lower()
        key = (dom, e.norm_key or norm_title(e.title or ""))
        if dom and key in seen:
            continue
        seen.add(key)
        deduped.append(e)
    return deduped


def group_rows(rows: list[CatalogWork], q: str | None = None) -> list[dict]:
    """Group rows into one entry per work (strong cross-source matching) with a list of
    selectable sources. Returns plain dicts (the router maps them to schemas)."""
    out: list[dict] = []
    for entries in _union_find_groups(rows):
        entries.sort(key=lambda e: _score(e, q), reverse=True)
        deduped = dedupe_sources(entries)
        # Card face: search relevance first, then the most prominent EDITION (popularity), then
        # data richness — so 'One Piece' leads its colored edition, consistent with the index rows
        # (_build_groups). Cover / synopsis fall back to any member's so a sparse rep still shows art.
        def _rep_key(e: CatalogWork) -> tuple:
            s = _score(e, q)
            return (s[0], e.popularity or 0.0, s[1], s[2], s[3])
        best = max(deduped, key=_rep_key)
        cover_url = best.cover_url or next((e.cover_url for e in deduped if e.cover_url), None)
        synopsis = best.synopsis or next((e.synopsis for e in deduped if e.synopsis), None)
        sources = [_source_dict(e) for e in deduped]
        out.append(
            {
                # Stable unique id for the group (the representative's catalog id) so the UI
                # has a collision-free React key — two same-titled works of different media
                # share a norm_key, which previously caused duplicated/again rendered cards.
                "id": best.id,
                "norm_key": best.norm_key or norm_title(best.title),
                "title": best.title,
                "author": best.author,
                "cover_url": cover_url,
                "synopsis": synopsis,
                "language": best.language,
                "media_kind": best.media_kind,
                "media_label": media_label(best),
                "is_adult": any(bool(getattr(e, "is_adult", False)) for e in deduped),
                # Representative popularity — used to ORDER groups (below) and as the precondition for
                # collapse_series_cards' "first seen = most prominent" rep pick. Ignored by the schema.
                "popularity": best.popularity or 0.0,
                "chapters": best.chapters_advertised or best.chapters_listed,
                "hooked_work_id": next(
                    (e.hooked_work_id for e in deduped if e.hooked_work_id), None
                ),
                # Series name when this work is part of a known series (gates the "View Series" UI).
                "series": _group_series(deduped),
                "sources": sources,
            }
        )
    # Sort groups: search relevance first, then POPULARITY (so a no-query browse leads with the most
    # popular titles, and collapse_series_cards' popularity-first precondition holds), then data
    # richness, then size. Popularity was previously omitted, so an obscure 600-chapter web-novel
    # outranked a famous 1-volume title and the series-card rep was the highest-chapter volume.
    def group_key(g: dict) -> tuple:
        ch = g["chapters"] or 0
        title_hit = 1 if (q and q.lower() in (g["title"] or "").lower()) else 0
        return (title_hit, g.get("popularity") or 0.0, bool(g["synopsis"]), ch)

    out.sort(key=group_key, reverse=True)
    return out


_READ_PREFIX_RE = re.compile(r"^(?:read|watch|listen to)\s+(?=\S)", re.I)


def recanonicalize_catalog(db: Session) -> dict:
    """One-time repair of crawled catalog rows using the *current* heuristics.

    Older crawls cataloged things the smarter classifier now rejects: each j-novel.club
    ``/read/<slug>-volume-N-part-M`` reader page became its own "Read <work>" entry, a
    site homepage became a bogus work, and Gutenberg bylines stayed glued to the title.
    This re-derives the canonical work URL / title / author for every ``web_index`` row,
    merges rows that now collapse onto the same work (carrying over the richest metadata),
    and drops site-root entries. Safe to run repeatedly (idempotent). Returns a summary."""
    rows = list(
        db.scalars(select(CatalogWork).where(CatalogWork.provider == "web_index")).all()
    )
    deleted_roots = merged = retitled = reauthored = 0

    groups: dict[tuple[int | None, str], list[CatalogWork]] = {}
    for e in rows:
        canon = work_url_for(e.work_url)
        # Drop pages that are not works at all: the site root/homepage, browse/genre/author/
        # bookshelf listing pages (e.g. Gutenberg /ebooks/author/<id>), and entries whose
        # title is just the site's own name or a generic chrome label ("Project Gutenberg").
        if (
            not urlparse(e.work_url).path.strip("/")
            or is_listing_url(canon)
            or is_junk_url(canon)
            or _is_site_name_title(e.title, None)
        ):
            db.delete(e)
            deleted_roots += 1
            continue
        groups.setdefault((e.site_id, canon), []).append(e)
    db.flush()

    for (_site_id, canon), entries in groups.items():
        # The surviving row is the one already AT the canonical URL (the real landing page),
        # else the richest by metadata score.
        entries.sort(key=lambda e: (e.work_url == canon, _score(e, None)), reverse=True)
        survivor = entries[0]
        for other in entries[1:]:
            survivor.author = survivor.author or other.author
            survivor.cover_url = survivor.cover_url or other.cover_url
            if other.synopsis and (
                not survivor.synopsis or len(other.synopsis) > len(survivor.synopsis)
            ):
                survivor.synopsis = other.synopsis
            survivor.chapters_advertised = (
                max(survivor.chapters_advertised or 0, other.chapters_advertised or 0) or None
            )
            survivor.chapters_listed = (
                max(survivor.chapters_listed or 0, other.chapters_listed or 0) or None
            )
            if other.hooked_work_id and not survivor.hooked_work_id:
                survivor.hooked_work_id = other.hooked_work_id
            if other.media_kind == "comic":
                survivor.media_kind = "comic"
            db.delete(other)
            merged += 1
        survivor.work_url = canon

        new_title = survivor.title
        if _is_gutenberg_book(canon) and not survivor.author:
            wt, author = split_byline(new_title)
            if author:
                new_title, survivor.author = wt, author
                reauthored += 1
        else:  # reader pages titled "Read <work>" → just the work title
            new_title = _READ_PREFIX_RE.sub("", new_title)
        new_title = (new_title or survivor.title)[:512]
        if new_title != survivor.title:
            survivor.title = new_title
            retitled += 1
        survivor.norm_key = norm_title(survivor.title)

    db.commit()
    return {
        "deleted_roots": deleted_roots,
        "merged": merged,
        "retitled": retitled,
        "reauthored": reauthored,
        "remaining": db.scalar(
            select(func.count(CatalogWork.id)).where(CatalogWork.provider == "web_index")
        ),
    }


def filter_and_sort_groups(
    groups: list[dict], *, media: str | None = None, domain: str | None = None,
    sort: str = "relevance",
) -> list[dict]:
    """Apply the Index page's media-type / source filters and sort to grouped catalog rows.
    Done over the FULL grouped set (server-side) so a low-ranked type/source (e.g. Gutenberg
    books) isn't silently dropped just because it fell outside the first page of results."""
    out = groups
    if media:
        out = [g for g in out if g.get("media_label") == media]
    if domain:
        out = [g for g in out if any(s.get("domain") == domain for s in g.get("sources", []))]
    if sort == "chapters":
        out = sorted(out, key=lambda g: g.get("chapters") or 0, reverse=True)
    elif sort == "title":
        out = sorted(out, key=lambda g: (g.get("title") or "").lower())
    # "relevance" → keep group_rows' existing ordering.
    return out


def collapse_series_cards(groups: list[dict]) -> list[dict]:
    """Fold per-volume cards of the SAME series into one representative card (14A alternative).

    The per-volume cards of a long series are the biggest source of browse over-cardinality, but the
    work-level CatalogGroup can't simply merge them: ``acquire`` treats every group member as a
    SOURCE of one work, so folding distinct volumes into one group would break acquisition. Instead
    this is a PRESENTATION-only fold applied to the browse list — the data model + acquire are
    untouched, each volume stays its own acquirable group (still reachable via search + View Series),
    and we just stop showing N near-identical cards.

    ``groups`` must already be in display order (popularity-first), so the FIRST volume seen for a
    series — the most prominent — becomes the representative; the rest collapse into it and bump its
    ``series_count``. The rep is re-titled to the series name so it reads as a series. Groups with no
    confident series, or a 'series' of one, pass through unchanged."""
    out: list[dict] = []
    reps: dict[tuple[str, str], dict] = {}
    for g in groups:
        name = (g.get("series") or "").strip()
        if not name:
            out.append(g)
            continue
        key = (norm_title(name), g.get("media_kind") or "")  # comics & prose of a same name stay apart
        rep = reps.get(key)
        if rep is None:
            g = {**g, "series_count": 1}
            reps[key] = g
            out.append(g)
        else:
            rep["series_count"] = rep.get("series_count", 1) + 1
    for g in out:                          # re-title only the cards that actually absorbed volumes
        if g.get("series_count", 1) > 1 and g.get("series"):
            g["title"] = g["series"]
    return out


def catalog_facets(db: Session, *, hide_books: bool = False) -> dict:
    """All distinct media types + source domains across the WHOLE catalog, so the Index page's
    filter dropdowns are complete. Derived from the precomputed grouping (media) + distinct source
    domains — NOT a row sample, which under a comix-dominated catalog never surfaced 'Book'/'Novel'.

    Also returns ``domain_media`` (domain → the media labels it carries) so the API can hide a
    source whose content is all in categories the user may not view (e.g. a Manga-only user should
    not be offered gutenberg.org / a novel site as a source filter).

    With ``hide_books`` (no acquisition pipeline) the pipeline-only book providers are excluded: a
    media label only appears if it still has a directly-hookable group, and book-provider source
    domains drop out of the domain list."""
    from collections import defaultdict

    from ..models import CatalogGroup
    media_q = select(CatalogGroup.media_label).distinct()
    # domain ↔ media-label pairs (via the group), so each source is tagged with what it carries.
    pair_q = (select(CatalogWork.domain, CatalogGroup.media_label)
              .join(CatalogGroup, CatalogWork.group_id == CatalogGroup.id)
              .where(CatalogWork.domain.isnot(None)).distinct())
    if hide_books:
        from .book_catalog import BOOK_PROVIDERS
        media_q = media_q.where(exists().where(
            (CatalogWork.group_id == CatalogGroup.id)
            & (CatalogWork.provider.notin_(BOOK_PROVIDERS))))
        pair_q = pair_q.where(CatalogWork.provider.notin_(BOOK_PROVIDERS))
    # Roll the fine media labels up to their Index categories (the comic labels → "Manga & Comics")
    # so the filter dropdown + per-source gating speak the same 3-way category vocabulary as the
    # sections and permissions.
    cats = {media_category(m) for (m,) in db.execute(media_q).all() if m}
    media = [c for c in MEDIA_CATEGORIES if c in cats]
    domain_media: dict[str, set[str]] = defaultdict(set)
    for dom, label in db.execute(pair_q).all():
        if dom and label:
            domain_media[dom].add(media_category(label))
    return {"media": media, "domains": sorted(domain_media),
            "domain_media": {d: sorted(v) for d, v in domain_media.items()}}


async def hook_entry(db: Session, entry: CatalogWork, *, start_chapter: int = 1) -> Work:
    """Move a catalog entry into the library: pull it via the adaptive web adapter,
    self-troubleshoot if no chapters surface, carry over the catalog's metadata, and
    record a completeness health verdict on both the Work and the catalog entry.

    ``start_chapter`` (1-based) hooks from a later chapter, skipping ones the user already read.
    Raises engine.ComplianceError (→ HTTP 403) if the adaptive-web source isn't enabled.
    """
    from . import blocklist, diagnose
    from .engine import ComplianceError, adapter_for, ensure_source, hook_work

    if blocklist.is_blocked(db, entry.work_url):
        raise ComplianceError("This title is on the operator blocklist and can't be hooked.")

    # Some domains have a purpose-built adapter (e.g. a site's own API) that ingests far
    # better than the generic adaptive-web crawler; route to it when recognized.
    source_key = _source_key_for(entry)
    src = ensure_source(db, registry.get(source_key))
    if not src.tos_permitted:
        label = registry.get(source_key).display_name
        raise ComplianceError(
            f"The '{label}' source must be enabled (and attested) on the Sources page "
            "before hooking this work."
        )

    work = await hook_work(db, source_key, entry.work_url, start_chapter=start_chapter)

    # Self-troubleshoot: we thought we found a title but discovery yielded no chapters.
    if not work.chapters:
        try:
            adapter = adapter_for(src)
            await diagnose.troubleshoot_discovery(db, work, adapter, entry.work_url)
            db.refresh(work)
        except Exception:  # noqa: BLE001 — diagnosis is best-effort
            db.rollback()

    # Carry catalog metadata the page-level discovery may have missed.
    if entry.cover_url and not work.cover_url:
        work.cover_url = entry.cover_url
    if entry.synopsis and not work.description:
        work.description = entry.synopsis
    if entry.author and not work.author:
        work.author = entry.author
    if entry.media_kind and work.media_kind == "text":
        work.media_kind = entry.media_kind

    entry.hooked_work_id = work.id
    report = diagnose.completeness(db, work)
    diagnose.apply_health(db, work, report)
    entry.health = report["health"]
    entry.health_detail = (report.get("detail") or "")[:1000] or None
    entry.diagnosed_at = _utcnow()
    db.commit()
    db.refresh(work)
    return work
