"""Smart catalog: cataloging discovered works + cross-site dedup/grouping + hook gate."""
from __future__ import annotations

import pytest
from sqlalchemy import delete, select

import app.ingestion.adapters  # noqa: F401  — populate the adapter registry
from app.db import SessionLocal, init_db
from app.ingestion import catalog
from app.ingestion.engine import ComplianceError
from app.models import CatalogWork, IndexedPage, IndexSite

# Reuse the regression fixtures so catalog + extract stay in lock-step.
from tests.test_extract import NOVELLUNAR_CHAPTER_HTML, NOVELLUNAR_NOVEL_HTML


@pytest.fixture(autouse=True)
def _clean_catalog():
    """Each test starts with an empty catalog (the test DB is shared across tests)."""
    init_db()
    db = SessionLocal()
    for model in (CatalogWork, IndexedPage, IndexSite):
        db.execute(delete(model))
    db.commit()
    db.close()
    yield


def _site(db, domain="novellunar.com") -> IndexSite:
    site = IndexSite(root_url=f"https://{domain}/", domain=domain, status="active",
                     max_pages=50, max_depth=3)
    db.add(site)
    db.commit()
    db.refresh(site)
    return site


def test_upsert_catalogs_a_novel_landing_page():
    init_db()
    db = SessionLocal()
    site = _site(db)
    url = "https://novellunar.com/novel/library-of-heavens-path-v1"
    entry = catalog.upsert_from_page(db, site, NOVELLUNAR_NOVEL_HTML, url)
    assert entry is not None
    assert entry.work_url == url
    assert "Library of Heaven" in entry.title
    assert entry.chapters_advertised == 2271
    assert entry.chapters_listed >= 4
    assert entry.synopsis and "Zhang Xuan" in entry.synopsis
    assert entry.cover_url and entry.cover_url.endswith("lohp.webp")
    assert entry.norm_key == "library of heavens path"
    db.close()


def test_chapter_page_attributes_to_parent_work():
    init_db()
    db = SessionLocal()
    site = _site(db)
    ch_url = "https://novellunar.com/novel/library-of-heavens-path-v1/chapter/1"
    entry = catalog.upsert_from_page(db, site, NOVELLUNAR_CHAPTER_HTML, ch_url)
    assert entry is not None
    # The catalog entry is keyed by the WORK url, not the chapter url.
    assert entry.work_url == "https://novellunar.com/novel/library-of-heavens-path-v1"
    # Only one entry exists for the work even though we fed a chapter page.
    assert db.scalar(select(CatalogWork).where(CatalogWork.site_id == site.id)) is entry
    db.close()


def test_non_literature_pages_are_not_catalogued():
    init_db()
    db = SessionLocal()
    site = _site(db, "example.org")
    assert catalog.upsert_from_page(
        db, site, "<html><body><p>hi</p></body></html>", "https://example.org/account/login"
    ) is None
    assert catalog.upsert_from_page(
        db, site, "<html><body>browse</body></html>", "https://example.org/browse/popular"
    ) is None
    assert db.scalar(select(CatalogWork)) is None
    db.close()


def test_landing_page_enriches_a_chapter_seeded_entry():
    init_db()
    db = SessionLocal()
    site = _site(db)
    ch_url = "https://novellunar.com/novel/library-of-heavens-path-v1/chapter/1"
    catalog.upsert_from_page(db, site, NOVELLUNAR_CHAPTER_HTML, ch_url)
    landing = "https://novellunar.com/novel/library-of-heavens-path-v1"
    entry = catalog.upsert_from_page(db, site, NOVELLUNAR_NOVEL_HTML, landing)
    # Same row, now enriched with the advertised count + synopsis from the landing page.
    assert db.scalar(select(CatalogWork)) is entry
    assert entry.chapters_advertised == 2271
    assert entry.synopsis
    db.close()


def test_group_rows_dedups_same_title_across_sites():
    init_db()
    db = SessionLocal()
    s1 = _site(db, "novellunar.com")
    s2 = _site(db, "othersite.net")
    catalog.upsert_from_page(db, s1, NOVELLUNAR_NOVEL_HTML,
                             "https://novellunar.com/novel/library-of-heavens-path-v1")
    # A second source with the same work, different cosmetic title.
    other = NOVELLUNAR_NOVEL_HTML.replace(
        "Library of Heaven's Path Novel", "The Library of Heaven's Path (Web Novel)"
    )
    catalog.upsert_from_page(db, s2, other, "https://othersite.net/book/lohp")

    rows = catalog.find_rows(db)
    groups = catalog.group_rows(rows)
    assert len(groups) == 1, [g["title"] for g in groups]
    assert len(groups[0]["sources"]) == 2
    domains = {s["domain"] for s in groups[0]["sources"]}
    assert domains == {"novellunar.com", "othersite.net"}
    db.close()


def test_catalog_search_matches_synopsis_and_title():
    init_db()
    db = SessionLocal()
    site = _site(db)
    catalog.upsert_from_page(db, site, NOVELLUNAR_NOVEL_HTML,
                             "https://novellunar.com/novel/library-of-heavens-path-v1")
    assert catalog.find_rows(db, q="heaven")          # title
    assert catalog.find_rows(db, q="Zhang Xuan")      # synopsis
    assert not catalog.find_rows(db, q="nonexistent zzzzz")
    db.close()


def test_search_candidate_limit_recovers_low_popularity_matches():
    """P2: a search must not drop low-popularity matches at the popularity-ranked candidate cap.
    With a small cap the obscure match falls off; widening the cap (as the search path now does)
    recovers it — so the index search candidate ceiling is well above the browse slice."""
    from app.models import CatalogWork

    from app.routers.index import _SEARCH_CANDIDATE_LIMIT

    init_db()
    db = SessionLocal()
    site = _site(db)
    db.execute(__import__("sqlalchemy").delete(CatalogWork)); db.commit()
    for i, pop in enumerate([100.0, 80.0, 60.0, 40.0, 1.0]):  # same title token, descending pop
        db.add(CatalogWork(site_id=site.id, domain=site.domain, media_kind="text",
                           work_url=f"https://x/zephyr/{i}", title=f"Zephyr Saga {i}",
                           norm_key=f"zephyr saga {i}", popularity=pop))
    db.commit()

    capped = catalog.find_rows(db, q="zephyr", limit=3)        # popularity cap drops the pop=1 match
    assert len(capped) == 3 and all("Zephyr" in r.title for r in capped)
    assert 1.0 not in {r.popularity for r in capped}
    wide = catalog.find_rows(db, q="zephyr", limit=_SEARCH_CANDIDATE_LIMIT)  # search path → all 5
    assert len(wide) == 5 and 1.0 in {r.popularity for r in wide}
    assert _SEARCH_CANDIDATE_LIMIT > 2000
    db.close()


def test_delete_work_clears_catalog_and_page_hooked_pointers():
    from app.models import IndexedPage, User, Work
    from app.routers.works import delete_work

    init_db()
    db = SessionLocal()
    site = _site(db)
    admin = User(username="admin_del", password_hash="x", role="admin")
    db.add(admin)
    work = Work(title="Hooked Title", hooked=True)
    db.add(work)
    db.commit()
    db.refresh(work)
    db.refresh(admin)
    cw = CatalogWork(site_id=site.id, domain=site.domain, work_url="https://x/n/1",
                     title="Hooked Title", norm_key="hooked title",
                     hooked_work_id=work.id, health="ok")
    page = IndexedPage(site_id=site.id, url="https://x/p/1", status="fetched",
                       hooked_work_id=work.id)
    db.add_all([cw, page])
    db.commit()

    # purge=True is the admin-only global delete that clears the shared work + its pointers.
    delete_work(work.id, purge=True, user_id=None, user=admin, db=db)

    db.refresh(cw)
    db.refresh(page)
    assert cw.hooked_work_id is None and cw.health == "unknown"
    assert page.hooked_work_id is None
    db.close()


@pytest.mark.asyncio
async def test_hook_entry_requires_enabled_adaptive_source():
    init_db()
    db = SessionLocal()
    site = _site(db)
    entry = catalog.upsert_from_page(db, site, NOVELLUNAR_NOVEL_HTML,
                                     "https://novellunar.com/novel/library-of-heavens-path-v1")
    # generic_feed defaults to tos_permitted=False → hooking is refused until enabled.
    with pytest.raises(ComplianceError):
        await catalog.hook_entry(db, entry)
    db.close()


def test_recanonicalize_repairs_legacy_rows():
    """The one-time repair: collapse j-novel /read parts onto the /series work, split a
    Gutenberg byline into title+author, and drop a site-root 'work'."""
    db = SessionLocal()
    jnovel = _site(db, "j-novel.club")
    gut = _site(db, "www.gutenberg.org")

    # Three j-novel reader-part pages that should collapse to one series work.
    base = "https://j-novel.club/read/reborn-to-reign-imposing-my-rules-with-my-mastery-of-magic"
    for part in (1, 2, 3):
        db.add(CatalogWork(
            site_id=jnovel.id, domain="j-novel.club", work_url=f"{base}-volume-1-part-{part}",
            title="Read Reborn to Reign", norm_key=catalog.norm_title("Read Reborn to Reign"),
        ))
    # A Gutenberg book with the byline glued into the title and no author.
    db.add(CatalogWork(
        site_id=gut.id, domain="www.gutenberg.org",
        work_url="https://www.gutenberg.org/ebooks/2701",
        title="Moby Dick; Or, The Whale by Herman Melville",
        norm_key=catalog.norm_title("Moby Dick; Or, The Whale by Herman Melville"),
    ))
    # A bogus site-root entry.
    db.add(CatalogWork(
        site_id=jnovel.id, domain="j-novel.club", work_url="https://j-novel.club",
        title="J-Novel Club", norm_key=catalog.norm_title("J-Novel Club"),
    ))
    db.commit()

    summary = catalog.recanonicalize_catalog(db)
    assert summary["deleted_roots"] == 1
    assert summary["merged"] == 2  # 3 parts → 1 survivor

    # The three parts are now a single series work.
    series = "https://j-novel.club/series/reborn-to-reign-imposing-my-rules-with-my-mastery-of-magic"
    rows = db.scalars(select(CatalogWork).where(CatalogWork.site_id == jnovel.id)).all()
    assert len(rows) == 1
    assert rows[0].work_url == series
    assert rows[0].title == "Reborn to Reign"

    moby = db.scalar(select(CatalogWork).where(CatalogWork.site_id == gut.id))
    assert moby.title == "Moby Dick; Or, The Whale"
    assert moby.author == "Herman Melville"
    db.close()


def test_collapse_series_cards_folds_volumes_for_browse():
    """14A alternative: per-volume cards of one series collapse to a single representative card in
    the browse, annotated with series_count + re-titled to the series; non-series cards and a
    same-name card in a DIFFERENT media bucket are untouched."""
    def g(title, series=None, kind="text"):
        return {"title": title, "series": series, "media_kind": kind,
                "norm_key": title.lower(), "series_count": 1, "sources": []}

    groups = [
        g("Mistborn: The Well of Ascension", series="Mistborn"),   # most-popular vol first → rep
        g("Mistborn: The Final Empire", series="Mistborn"),
        g("Mistborn: The Hero of Ages", series="Mistborn"),
        g("Mistborn", series="Mistborn", kind="comic"),           # the manga — different bucket
        g("Standalone Book", series=None),
        g("Warbreaker", series=""),                                # empty series → passes through
    ]
    out = catalog.collapse_series_cards(groups)
    # 3 prose volumes → 1 card; the manga stays its own; 2 non-series pass through → 4 total.
    assert len(out) == 4
    rep = next(x for x in out if x.get("series") == "Mistborn" and x["media_kind"] == "text")
    assert rep["series_count"] == 3 and rep["title"] == "Mistborn"
    assert any(x["media_kind"] == "comic" and x.get("series_count", 1) == 1 for x in out)
    assert {x["title"] for x in out} >= {"Standalone Book", "Warbreaker"}


def test_novel_and_manga_do_not_group_together():
    """A light novel and its manga adaptation share a title but are different works —
    they must stay as separate cards (grouping splits on media class)."""
    db = SessionLocal()
    s = _site(db, "example.com")
    db.add(CatalogWork(site_id=s.id, domain="example.com", media_kind="text",
                       work_url="https://example.com/novel/villainess", title="My Next Life as a Villainess",
                       norm_key=catalog.norm_title("My Next Life as a Villainess")))
    db.add(CatalogWork(site_id=s.id, domain="example.com", media_kind="comic",
                       work_url="https://example.com/manga/villainess", title="My Next Life as a Villainess",
                       norm_key=catalog.norm_title("My Next Life as a Villainess")))
    db.commit()
    rows = catalog.find_rows(db)
    groups = catalog.group_rows(rows)
    assert len(groups) == 2, [g["title"] for g in groups]
    labels = {g["media_label"] for g in groups}
    assert "Novel" in labels and "Manga" in labels
    db.close()


def test_editions_group_together_but_spinoffs_stay_separate():
    """Colored vs B/W are EDITIONS of the same work: they group into one card with both as
    selectable sources (even on the same domain). A same-franchise spin-off ('One Piece Party')
    is a different work and must be its own card — Jaccard alone over-merged them before."""
    init_db()
    db = SessionLocal()
    s = _site(db, "comix.to")
    for title, url in [
        ("One Piece", "https://comix.to/title/op-bw"),
        ("One Piece (Official Colored)", "https://comix.to/title/op-colored"),
        ("One Piece Party", "https://comix.to/title/op-party"),
    ]:
        db.add(CatalogWork(site_id=s.id, domain="comix.to", media_kind="comic", work_url=url,
                           title=title, norm_key=catalog.norm_title(title)))
    db.commit()
    groups = catalog.group_rows(catalog.find_rows(db))
    by_title = {g["title"]: g for g in groups}
    # Spin-off is its own card; the two editions share one card.
    assert "One Piece Party" in by_title
    edition_card = next(g for g in groups if g["title"] in ("One Piece", "One Piece (Official Colored)"))
    assert len(groups) == 2, [g["title"] for g in groups]
    # Both editions survive dedupe (same domain, distinct works) and are selectable.
    src_titles = {s["title"] for s in edition_card["sources"]}
    assert src_titles == {"One Piece", "One Piece (Official Colored)"}
    db.close()


def test_media_label_classifies_sources():
    from app.models import CatalogWork as CW
    assert catalog.media_label(CW(domain="www.gutenberg.org", media_kind="text", title="X")) == "Book"
    # "Novel" is reserved for web / light / Asian-style novels — crawled web_index sites and the
    # light-novel providers; general book providers (incl. Hardcover) are "Book".
    assert catalog.media_label(
        CW(domain="novellunar.com", provider="web_index", media_kind="text", title="X")) == "Novel"
    assert catalog.media_label(
        CW(domain="ranobedb.org", provider="ranobedb", media_kind="text", title="X")) == "Novel"
    assert catalog.media_label(
        CW(domain="hardcover.app", provider="hardcover", media_kind="text", title="X")) == "Book"
    assert catalog.media_label(CW(domain="example.org", media_kind="comic", title="X manga")) == "Manga"
    # Manhua (Chinese) and manhwa/webtoon (Korean) are their own categories, not lumped into Manga.
    assert catalog.media_label(CW(domain="example.org", media_kind="comic", title="Some Manhua")) == "Manhua"
    assert catalog.media_label(CW(domain="example.org", media_kind="comic", title="Some Manhwa")) == "Webtoon"
    assert catalog.media_label(CW(domain="webtoons.com", media_kind="comic", title="X")) == "Webtoon"
    assert catalog.media_label(CW(domain="example.org", media_kind="comic", title="Generic")) == "Comic"
    # comix.to API type wins over title hints.
    assert catalog.media_label(CW(domain="comix.to", media_kind="comic", title="X",
                                  extra={"comix_type": "manhua"})) == "Manhua"
    # Untyped comix.to entry (no comix_type, no title hint) → Manga (the aggregator's dominant type),
    # not the generic "Comic".
    assert catalog.media_label(CW(domain="comix.to", provider="web_index", work_url="https://comix.to/t/x",
                                  media_kind="comic", title="Generic")) == "Manga"
    # An AUTHORITATIVE metadata label (set by metadata_sync from AniList's format) overrides the
    # URL/title heuristic — even when the heuristic would say something else.
    assert catalog.media_label(CW(domain="novellunar.com", provider="web_index", media_kind="text",
                                  title="Solo Leveling", extra={"meta_label": "Webtoon"})) == "Webtoon"
    # A bogus/invalid meta_label is ignored (falls back to the heuristic).
    assert catalog.media_label(CW(domain="www.gutenberg.org", media_kind="text", title="X",
                                  extra={"meta_label": "Bogus"})) == "Book"
