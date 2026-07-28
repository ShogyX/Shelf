"""Categorization: the media filter, and the chain that badged Roald Dahl as Manga.

Traced on the live library: two prose catalog rows for "Charlie and the Chocolate Factory" were
hooked to the manhua work "Tales Of Demons And Gods", AniList's label was then stamped onto every
row hooked to that work, and the group label took it — so the Dahl card was filed under Manga.
Three layers, each fixed and each pinned here, plus the filter that made comics unreachable.
"""
from __future__ import annotations

import pytest
from sqlalchemy import delete, select

from app.db import SessionLocal, init_db
from app.ingestion import catalog
from app.ingestion.catalog_groups import _group_label
from app.models import CatalogGroup, CatalogWork, Chapter, ChapterContent, Work


# ---------------------------------------------------------------- the media filter (all comics)
def test_selecting_the_comics_category_matches_every_comic_label():
    """The dropdown sends a CATEGORY; groups carry a fine LABEL. Comparing them directly matched
    nothing, so 'Manga & Comics' returned an empty catalog while ~124k comic groups existed."""
    groups = [{"media_label": lbl} for lbl in ("Manga", "Manhua", "Webtoon", "Comic", "Novel", "Book")]
    got = catalog.filter_and_sort_groups(groups, media="Manga & Comics", domain=None, sort=None)
    assert sorted(g["media_label"] for g in got) == ["Comic", "Manga", "Manhua", "Webtoon"]
    # A bare label still filters to itself, and the two that coincide with a category still work.
    for lbl in ("Novel", "Book", "Manga"):
        out = catalog.filter_and_sort_groups(groups, media=lbl, domain=None, sort=None)
        assert [g["media_label"] for g in out] == [lbl]


# --------------------------------------------------------- layer 3: the group label (display)
def _cw(title, kind="text", meta_label=None, rid=1, provider="openlibrary"):
    """A real (unpersisted) CatalogWork — the label heuristics read several columns, so a stub
    silently takes different branches than production."""
    row = CatalogWork(id=rid, provider=provider, provider_ref=f"r{rid}", domain="d",
                      work_url=f"u{rid}", title=title, media_kind=kind, popularity=1.0)
    row.extra = {"meta_label": meta_label} if meta_label else {}
    return row


def test_a_comic_label_cannot_repaint_a_prose_group():
    """The Dahl symptom: one member carrying meta_label='Manga' relabelled the whole prose group."""
    prose = [_cw("Charlie and the Chocolate Factory", meta_label="Manga", rid=1),
             _cw("Charlie and the Chocolate Factory", rid=2)]
    assert _group_label(prose, prose[0]) not in ("Manga", "Manhua", "Webtoon", "Comic")
    # ...but a genuine comic group still takes its label.
    comic = [_cw("Solo Leveling", kind="comic", meta_label="Manga", rid=3)]
    assert _group_label(comic, comic[0]) == "Manga"
    # A non-comic authoritative label is unaffected on a prose group.
    novel = [_cw("Re:Zero", meta_label="Novel", rid=4)]
    assert _group_label(novel, novel[0]) == "Novel"


# ------------------------------------------------- layer 2: label propagation across a hook set
def test_the_provider_label_only_lands_on_rows_that_are_the_same_title():
    """metadata_sync stamps its label onto every row hooked to a work. One wrong hook therefore
    turned into a wrong BADGE on unrelated titles — a prose Dahl row inherited 'Manga' from a
    manhua it was wrongly hooked to."""
    from app.integrations.metadata_sync import _apply_meta_label
    from app.integrations.metadata import ProviderMeta

    init_db()
    db = SessionLocal()
    try:
        db.execute(delete(CatalogWork)); db.execute(delete(Work)); db.commit()
        work = Work(title="Tales Of Demons And Gods", author="Fabiao De Woniu", media_kind="comic")
        db.add(work); db.commit(); db.refresh(work)
        right = CatalogWork(provider="anilist", provider_ref="r1", domain="d", work_url="u1",
                            title="Tales Of Demons And Gods", media_kind="comic",
                            hooked_work_id=work.id)
        wrong = CatalogWork(provider="openlibrary", provider_ref="r2", domain="d", work_url="u2",
                            title="Charlie and the Chocolate Factory", author="Roald Dahl",
                            media_kind="text", hooked_work_id=work.id)
        db.add_all([right, wrong]); db.commit()

        _apply_meta_label(db, work, ProviderMeta(ref="x", title="Tales Of Demons And Gods", media_kind="comic",
                                            extra={"format": "MANGA"}))
        db.commit()
        db.refresh(right); db.refresh(wrong)

        assert (right.extra or {}).get("meta_label") == "Manga"
        assert (wrong.extra or {}).get("meta_label") is None, "labelled an unrelated title"
        assert wrong.media_kind == "text", "flipped an unrelated prose row to comic"
    finally:
        db.close()


# ------------------------------------------------------- layer 1: the hook roll-down itself
def test_a_group_hook_only_rolls_down_to_members_that_match():
    """_set_hook used to bulk-UPDATE every member sharing the group_id, so one bad cluster
    membership permanently pointed unrelated titles at the wrong work. This is the root cause."""
    from app.ingestion.stock_link import link_catalog_to_stock

    init_db()
    db = SessionLocal()
    try:
        for m in (ChapterContent, Chapter, CatalogWork, CatalogGroup, Work):
            db.execute(delete(m))
        db.commit()
        # A stocked, readable work is required for it to be a hook candidate.
        import tempfile, os
        d = tempfile.mkdtemp()
        f = os.path.join(d, "tales.epub")
        open(f, "wb").write(b"x")
        work = Work(title="Tales Of Demons And Gods", media_kind="comic", local_path=f)
        db.add(work); db.commit(); db.refresh(work)
        # Only a READABLE work counts as stock (see the dead_stock predicate alignment).
        ch = Chapter(work_id=work.id, source_chapter_ref="c1", index=1, title="Ch 1",
                     fetch_status="fetched")
        db.add(ch); db.flush()
        cc = ChapterContent(chapter_id=ch.id, format="html", body="<p>x</p>",
                            word_count=1, checksum="k1")
        db.add(cc); db.flush(); ch.content_id = cc.id
        db.commit()

        grp = CatalogGroup(id=900, norm_key="tales of demons and gods", media_bucket="comic",
                           title="Tales Of Demons And Gods", media_label="Manga", popularity_norm=1.0)
        db.add(grp); db.commit()
        member_ok = CatalogWork(provider="comix", provider_ref="m1", domain="d", work_url="u1",
                                title="Tales Of Demons And Gods", media_kind="comic", group_id=900)
        member_bad = CatalogWork(provider="openlibrary", provider_ref="m2", domain="d", work_url="u2",
                                 title="Charlie and the Chocolate Factory", author="Roald Dahl",
                                 media_kind="text", group_id=900)
        db.add_all([member_ok, member_bad]); db.commit()

        link_catalog_to_stock(db)
        db.commit()
        db.refresh(member_ok); db.refresh(member_bad)

        assert member_ok.hooked_work_id == work.id, "the matching member should have been hooked"
        assert member_bad.hooked_work_id is None, "an unrelated member inherited the group's hook"
    finally:
        db.close()
