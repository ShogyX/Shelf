"""Crawled serialized titles: the MOST COMPLETE listing should win.

A manga indexed on two crawl sources is routinely 1100 chapters on one and 50 on the other. Which
one a title got hooked to used to be whichever the row scan returned first — i.e. luck — and the
card showed the display representative's chapter count, which is chosen for title/popularity and
says nothing about completeness.
"""
from __future__ import annotations

from app.ingestion.acquire import crawl_completeness


class _M:
    def __init__(self, id, listed=None, advertised=None):
        self.id, self.chapters_listed, self.chapters_advertised = id, listed, advertised


def _best(members):
    return max(members, key=lambda m: (*crawl_completeness(m), -m.id))


def test_the_listing_with_more_real_chapters_wins():
    sparse, full = _M(1, listed=50), _M(2, listed=1100)
    assert _best([sparse, full]) is full
    assert _best([full, sparse]) is full          # independent of scan order


def test_enumerated_chapters_beat_an_advertised_claim():
    """chapters_listed is what the crawler actually found; chapters_advertised is the site's boast.
    A source claiming 1200 must not outrank one that genuinely lists 1100."""
    boastful, real = _M(1, listed=0, advertised=1200), _M(2, listed=1100, advertised=1100)
    assert _best([boastful, real]) is real


def test_advertised_only_breaks_ties():
    """A freshly-indexed source may advertise before anything is listed — still rank it."""
    a, b = _M(1, listed=0, advertised=10), _M(2, listed=0, advertised=900)
    assert _best([a, b]) is b


def test_ordering_is_total_so_repeated_calls_agree():
    a, b = _M(7, listed=100), _M(3, listed=100)
    assert _best([a, b]) is _best([b, a])          # a tie resolves the same way every time


def test_missing_counts_do_not_crash_and_rank_last():
    unknown, known = _M(1), _M(2, listed=5)
    assert crawl_completeness(unknown) == (0, 0)
    assert _best([unknown, known]) is known

def test_group_chapter_count_reports_the_best_member():
    """The card must not under-report. The rep is picked for DISPLAY identity (English title,
    popularity) — a sparse listing winning the display used to drag the whole card's count down."""
    from app.ingestion.catalog_groups import _build_groups
    from app.models import CatalogWork

    def row(cid, title, pop, advertised):
        return CatalogWork(id=cid, provider="web_index", provider_ref=f"r{cid}", domain="d",
                           work_url=f"u{cid}", title=title, norm_key="onepiece",
                           media_kind="comic", language="en", popularity=pop,
                           chapters_advertised=advertised)

    # The POPULAR listing is the sparse one, so it wins the display rep — the completeness must not
    # follow it down.
    groups = _build_groups([row(1, "One Piece", 99.0, 50), row(2, "One Piece", 1.0, 1100)])
    assert len(groups) == 1
    assert groups[0]["chapters"] == 1100
