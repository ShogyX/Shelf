"""Regression tests for the 2026-06-13 review fixes (see REVIEW_2026-06-13.txt).

Covers the matching / series / ingestion / catalog correctness fixes so they don't silently regress:
  * non-Latin author disambiguation (false-merge guard)
  * norm_title never erases a whole title to a blank grouping key
  * work_title_from preserves an intrinsic colon (Re:Zero)
  * the no-author confidence gate exempts comics + fuzzing
  * "set"/"books" no longer misflag a real book as a boxset
  * browse groups order by popularity, not chapter count
  * series transient-failure negatives are not durably cached
  * the stock auto-selection language gate accepts every English variant
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from app.db import SessionLocal, init_db
from app.ingestion import catalog, release_matcher as rm, series
from app.ingestion.extract import authors_compatible, norm_title, work_title_from
from app.models import CatalogGroup, CatalogWork, StockItem


# --------------------------------------------------------------- extract / norm_title
def test_authors_compatible_distinguishes_non_latin_names():
    # Two DIFFERENT mangaka must NOT be treated as compatible (the old ASCII-only norm emptied both
    # to "" and returned True, mis-merging same-title-different-author works).
    assert authors_compatible("村田 雄介", "尾田 栄一郎") is False
    assert authors_compatible("尾田 栄一郎", "尾田 栄一郎") is True
    # Accent folding (a bonus of the fix) + Latin behaviour preserved.
    assert authors_compatible("José Saramago", "Jose Saramago") is True
    assert authors_compatible("Brandon Sanderson", "Stephen King") is False
    # Unknown on either side still doesn't block a title match.
    assert authors_compatible(None, "Anyone") is True


def test_norm_title_never_empties_a_standalone_title():
    # Single-letter volume markers + the trailing "- NN" rule must not erase the whole title to a
    # blank grouping key (which the union-find then refuses to group → stranded title).
    for t in ("V 2", "S 1", "C 137"):
        assert norm_title(t), f"{t!r} normalized to an empty key"
    # ...while real volume markers STILL collapse a per-volume series to one key.
    assert norm_title("Berserk Vol 1") == "berserk"
    assert norm_title("Berserk v2") == "berserk"
    assert norm_title("Naruto - 7") == "naruto"


def test_work_title_from_keeps_intrinsic_colon():
    assert work_title_from("Re:Zero kara Hajimeru Isekai Seikatsu") == "Re:Zero kara Hajimeru Isekai Seikatsu"
    # A spaced colon is still a subtitle separator.
    assert work_title_from("Dune: Part One") == "Dune"
    assert work_title_from("Spider-Man: No Way Home") == "Spider-Man"


# --------------------------------------------------------------- release matching
@dataclass
class FakeRelease:
    title: str
    size: int = 10_000_000
    categories: list = field(default_factory=lambda: [7030])  # comic category
    grabs: int = 0
    download_url: str | None = "http://idx/nzb"


def test_no_author_gate_exempts_comics():
    """A comic CatalogWork carries no author; the 0.9 no-author gate must NOT reject it (it would
    kill the whole comic pipeline). Title "Project Hail Mary" → sig {project,hail,mary}; a release
    missing one token → recall 0.667 (≥ floor 0.6, < 0.9)."""
    comic_prefs = rm.search_prefs(None, media_kind="comic")
    assert comic_prefs["is_comic"] is True
    rel = FakeRelease("Project Hail v01 (2012) (Digital)")  # "mary" missing → recall 2/3
    sr = rm.score_release("Project Hail Mary", None, None, rel, comic_prefs, want_bucket="comic")
    assert sr.accepted, sr.reasons
    # The same author-less title at the same recall as PROSE is held to the near-exact 0.9 bar.
    prose_prefs = rm.search_prefs(None, media_kind="text")
    assert prose_prefs.get("is_comic") in (False, None)
    sr2 = rm.score_release("Project Hail Mary", None, None,
                           FakeRelease("Project Hail EPUB", categories=[7000]), prose_prefs)
    assert not sr2.accepted, sr2.reason


def test_no_author_gate_skipped_when_fuzzing():
    prose_prefs = rm.search_prefs(None, media_kind="text")
    rel = FakeRelease("Project Hail EPUB", categories=[7000])  # recall 0.667, author-less prose
    # At the explicit fuzz floor the operator wants the long tail tried — the 0.9 gate must not apply.
    sr = rm.score_release("Project Hail Mary", None, None, rel, prose_prefs, floor=0.3)
    assert sr.accepted, sr.reason


def test_generic_set_books_not_flagged_boxset():
    assert rm.parse_release("Set Me Free EPUB").is_boxset is False
    assert rm.parse_release("Books of Blood by Clive Barker EPUB").is_boxset is False
    # Genuine bundles still detected (via box/boxset/the numeric range).
    assert rm.parse_release("Box Set The Complete Trilogy").is_boxset is True
    assert rm.parse_release("Naruto Books 1-3 Omnibus").is_boxset is True


# --------------------------------------------------------------- catalog ordering
def _row(i: int, title: str, pop: float, chapters: int) -> CatalogWork:
    return CatalogWork(id=i, domain="x.com", work_url=f"https://x.com/{i}", title=title,
                       norm_key=norm_title(title), media_kind="text", popularity=pop,
                       chapters_advertised=chapters)


def test_group_rows_orders_by_popularity_not_chapters():
    # A famous low-chapter title must outrank an obscure high-chapter one in a no-query browse.
    rows = [_row(1, "Obscure Webnovel", pop=1.0, chapters=600),
            _row(2, "Famous Book", pop=999.0, chapters=3)]
    groups = catalog.group_rows(rows, q=None)
    assert [g["title"] for g in groups][0] == "Famous Book", [g["title"] for g in groups]


# --------------------------------------------------------------- series transient negative cache
def test_series_transient_failure_not_durably_cached(monkeypatch):
    init_db()
    db = SessionLocal()
    cw = CatalogWork(domain="x.com", work_url="https://x.com/s", title="Some Book",
                     norm_key="some book", media_kind="text")
    db.add(cw); db.commit()

    persisted: list = []
    monkeypatch.setattr(series, "_hc_token", lambda _db: None)

    async def _fake_hc(*a, **k):
        return (None, [])

    async def _fake_name(*a, **k):
        series._mark_transient()   # simulate a provider blip during name resolution
        return None

    monkeypatch.setattr(series, "_hc_series_lookup", _fake_hc)
    monkeypatch.setattr(series, "_series_name_for", _fake_name)
    monkeypatch.setattr(series, "_persist_series_members",
                        lambda *a, **k: persisted.append(a))

    import asyncio
    out = asyncio.run(series.detect_series(db, cw))
    assert out == {"series": None, "books": []}
    assert persisted == [], "a transient-failure negative must not be durably persisted"
    db.close()


def test_series_genuine_negative_is_cached(monkeypatch):
    init_db()
    db = SessionLocal()
    cw = CatalogWork(domain="x.com", work_url="https://x.com/s2", title="Lonely Book",
                     norm_key="lonely book", media_kind="text")
    db.add(cw); db.commit()

    persisted: list = []
    monkeypatch.setattr(series, "_hc_token", lambda _db: None)

    async def _fake_hc(*a, **k):
        return (None, [])

    async def _fake_name(*a, **k):
        return None   # genuine "no series" — no transient mark

    monkeypatch.setattr(series, "_hc_series_lookup", _fake_hc)
    monkeypatch.setattr(series, "_series_name_for", _fake_name)
    monkeypatch.setattr(series, "_persist_series_members",
                        lambda *a, **k: persisted.append(a))

    import asyncio
    out = asyncio.run(series.detect_series(db, cw))
    assert out == {"series": None, "books": []}
    assert len(persisted) == 1, "a genuine negative SHOULD be cached"
    db.close()


# --------------------------------------------------------------- stock language gate
def test_stock_language_gate_accepts_english_variants():
    from app.ingestion.stock import _select_groups
    init_db()
    db = SessionLocal()
    for m in (StockItem, CatalogGroup):
        db.execute(__import__("sqlalchemy").delete(m))
    db.commit()
    langs = {"english": "English", "en-ca": "en-CA", "eng": "eng", "ja": "ja"}
    for slug, lang in langs.items():
        db.add(CatalogGroup(norm_key=f"k-{slug}", media_bucket="text", title=f"T {slug}",
                            language=lang, popularity_norm=1.0))
    db.commit()
    picked = _select_groups(db, media=None, dimension=None, value=None,
                            sort="popularity", limit=50, group_ids=None)
    titles = {g.title for g in picked}
    assert "T english" in titles and "T en-ca" in titles and "T eng" in titles
    assert "T ja" not in titles  # foreign title still excluded
    db.close()
