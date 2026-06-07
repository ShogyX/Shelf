"""Unit tests for the catalog-book ↔ Prowlarr-release matching engine."""
from __future__ import annotations

from dataclasses import dataclass, field

from app.ingestion import release_matcher as rm


@dataclass
class FakeRelease:
    title: str
    size: int = 10_000_000
    categories: list = field(default_factory=lambda: [7000, 7020])
    grabs: int = 0
    download_url: str | None = "http://idx/nzb"


def _prefs(**over):
    base = rm.search_prefs(None)  # defaults
    base.update(over)
    return base


def test_parse_ebook_release():
    info = rm.parse_release("Andy.Weir-Project.Hail.Mary.A.Novel.2021.Retail.EPUB.eBook-BitBook")
    assert info.fmt == "epub" and not info.is_audiobook and info.is_retail
    assert "project" in info.content_tokens and "weir" in info.content_tokens
    assert "2021" not in info.content_tokens  # year stripped
    assert "epub" not in info.content_tokens and "novel" not in info.content_tokens


def test_parse_audiobook_by_category_and_token():
    a = rm.parse_release("[M4B] Andy Weir - Project Hail Mary", categories=[3030])
    assert a.is_audiobook and a.fmt == "audio"
    b = rm.parse_release("Andy Weir - Project Hail Mary (Unabridged) MP3")
    assert b.is_audiobook and b.fmt == "audio"


def test_parse_language():
    info = rm.parse_release("Andy Weir - Project Hail Mary German EPUB")
    assert info.language == "de"


def test_confidence_author_gate():
    info = rm.parse_release("Andy.Weir-Project.Hail.Mary.EPUB")
    assert rm.title_author_confidence("Project Hail Mary", "Andy Weir", info) >= 0.95
    # title present but author absent → penalized
    noauth = rm.parse_release("Project.Hail.Mary.EPUB")
    c = rm.title_author_confidence("Project Hail Mary", "Andy Weir", noauth)
    assert 0.0 < c < 0.95
    # single-token title without the author → rejected (too risky)
    dune = rm.parse_release("Dune.2021.EPUB")
    assert rm.title_author_confidence("Dune", "Frank Herbert", dune) == 0.0
    dune_ok = rm.parse_release("Frank.Herbert-Dune.EPUB")
    assert rm.title_author_confidence("Dune", "Frank Herbert", dune_ok) == 1.0


def test_score_gates():
    prefs = _prefs(preferred_formats=["epub", "azw3"], exclude_terms=["sample"],
                   min_size_mb=1, max_size_mb=50)
    good = rm.score_release("Project Hail Mary", "Andy Weir", "en",
                            FakeRelease("Andy.Weir-Project.Hail.Mary.Retail.EPUB"), prefs)
    assert good.accepted and good.auto_ok and good.info.fmt == "epub"

    excl = rm.score_release("Project Hail Mary", "Andy Weir", "en",
                            FakeRelease("Andy.Weir-Project.Hail.Mary.EPUB.sample"), prefs)
    assert not excl.accepted

    badfmt = rm.score_release("Project Hail Mary", "Andy Weir", "en",
                              FakeRelease("Andy.Weir-Project.Hail.Mary.PDF"), prefs)
    assert not badfmt.accepted  # pdf not in preferred list

    toobig = rm.score_release("Project Hail Mary", "Andy Weir", "en",
                              FakeRelease("Andy.Weir-Project.Hail.Mary.EPUB", size=900_000_000), prefs)
    assert not toobig.accepted


def test_audiobook_gate():
    ebook_only = _prefs(categories=[7000, 7020])  # want_audiobooks False
    sr = rm.score_release("Project Hail Mary", "Andy Weir", "en",
                          FakeRelease("[M4B] Andy Weir - Project Hail Mary", categories=[3030]),
                          ebook_only)
    assert not sr.accepted
    audio_ok = _prefs(want_audiobooks=True, want_ebooks=False)
    sr2 = rm.score_release("Project Hail Mary", "Andy Weir", "en",
                           FakeRelease("[M4B] Andy Weir - Project Hail Mary", categories=[3030]),
                           audio_ok)
    assert sr2.accepted


def test_rank_dedups_and_orders():
    prefs = _prefs(preferred_formats=["epub", "mobi", "pdf"])
    rels = [
        FakeRelease("Andy.Weir-Project.Hail.Mary.EPUB", grabs=10),
        FakeRelease("Andy.Weir-Project.Hail.Mary.EPUB", grabs=10),   # duplicate
        FakeRelease("Andy.Weir-Project.Hail.Mary.MOBI"),
        FakeRelease("Unrelated.Book.By.Someone.EPUB"),
    ]
    ranked = rm.rank_releases("Project Hail Mary", "Andy Weir", "en", rels, prefs)
    titles = [r.release.title for r in ranked]
    assert titles.count("Andy.Weir-Project.Hail.Mary.EPUB") == 1   # deduped
    assert "Unrelated.Book.By.Someone.EPUB" not in titles          # filtered (no match)
    assert ranked[0].info.fmt == "epub"                            # epub preferred over mobi


def test_auto_ok_requires_download_url_and_confidence():
    prefs = _prefs(auto_grab_min_confidence=0.8)
    no_url = rm.score_release("Project Hail Mary", "Andy Weir", "en",
                              FakeRelease("Andy.Weir-Project.Hail.Mary.EPUB", download_url=None), prefs)
    assert no_url.accepted and not no_url.auto_ok
    weak = rm.score_release("Project Hail Mary", "Andy Weir", "en",
                            FakeRelease("Project.Hail.Mary.EPUB"), prefs)  # no author → ~0.6
    assert not weak.auto_ok


def test_build_query():
    assert rm.build_query("Project Hail Mary", "Andy Weir") == "project hail mary weir"
    # surname already in title → don't duplicate
    q = rm.build_query("Dune", "Frank Herbert")
    assert q == "dune herbert"
    # "Last, First" formatting → use the surname before the comma
    assert rm.build_query("Dune", "Herbert, Frank") == "dune herbert"


# --- Auto-grab safety (fully-automatic grabbing → these must NEVER auto-grab the wrong thing) ---
def _prefs_strict():
    return _prefs(preferred_formats=["epub", "azw3", "mobi"], languages=["en"])


def test_sequel_volume_not_auto_grabbed():
    prefs = _prefs_strict()
    sr = rm.score_release("Mistborn", "Brandon Sanderson", "en",
                          FakeRelease("Brandon.Sanderson-Mistborn.The.Hero.of.Ages.EPUB"), prefs)
    assert sr.accepted and not sr.auto_ok  # related, but a different volume → no auto-grab


def test_boxset_not_auto_grabbed():
    prefs = _prefs_strict()
    for name in ("Brandon.Sanderson-Mistborn.Trilogy.Omnibus.EPUB",
                 "Brandon.Sanderson-Mistborn.Books.1-3.EPUB"):
        sr = rm.score_release("Mistborn", "Brandon Sanderson", "en", FakeRelease(name), prefs)
        assert not sr.auto_ok, name


def test_explicit_volume_not_auto():
    prefs = _prefs_strict()
    sr = rm.score_release("The Wheel of Time", "Robert Jordan", "en",
                          FakeRelease("Robert.Jordan-The.Wheel.of.Time.Book.4.EPUB"), prefs)
    assert not sr.auto_ok


def test_companion_rejected():
    prefs = _prefs_strict()
    for name in ("Project.Hail.Mary.A.Summary.and.Analysis.EPUB",
                 "Project.Hail.Mary.Study.Guide.EPUB"):
        sr = rm.score_release("Project Hail Mary", "Andy Weir", "en", FakeRelease(name), prefs)
        assert not sr.accepted, name


def test_comics_category_does_not_bypass_format_gate():
    prefs = _prefs(categories=[7060], preferred_formats=["epub"])  # want_ebooks must stay True
    assert prefs["want_ebooks"] is True
    sr = rm.score_release("Project Hail Mary", "Andy Weir", "en",
                          FakeRelease("Andy.Weir-Project.Hail.Mary.PDF", categories=[7060]), prefs)
    assert not sr.accepted  # pdf not in preferred → still gated


def test_non_english_untagged_not_auto():
    prefs = _prefs(languages=["en"])
    sr = rm.score_release("Der Steppenwolf", "Hermann Hesse", "de",
                          FakeRelease("Hermann.Hesse-Der.Steppenwolf.EPUB"), prefs)
    assert not sr.auto_ok  # known non-English book + release doesn't confirm language


def test_declared_wrong_language_rejected():
    prefs = _prefs(languages=["en"])
    sr = rm.score_release("Project Hail Mary", "Andy Weir", "en",
                          FakeRelease("Andy.Weir-Project.Hail.Mary.German.EPUB"), prefs)
    assert not sr.accepted


def test_bare_and_roman_and_range_volumes_not_auto():
    prefs = _prefs_strict()
    cases = [
        ("Dennis.Taylor-Bobiverse.02.EPUB", "Bobiverse", "Dennis Taylor"),         # bare number
        ("Frank.Herbert-Dune.II.EPUB", "Dune", "Frank Herbert"),                   # roman numeral
        ("Brandon.Sanderson-Mistborn.1-4.EPUB", "Mistborn", "Brandon Sanderson"),  # range/omnibus
        ("Dennis.Taylor-Bobiverse.#2.EPUB", "Bobiverse", "Dennis Taylor"),         # #N
    ]
    for name, title, author in cases:
        sr = rm.score_release(title, author, "en", FakeRelease(name), prefs)
        assert not sr.auto_ok, name


def test_numeric_title_still_auto_grabs():
    # A number that IS part of the title must not block auto-grab.
    prefs = _prefs_strict()
    sr = rm.score_release("Fahrenheit 451", "Ray Bradbury", "en",
                          FakeRelease("Ray.Bradbury-Fahrenheit.451.Retail.EPUB-NODE"), prefs)
    assert sr.accepted and sr.auto_ok


def test_series_context_relaxes_for_known_volume():
    """A known series volume: the release names the series + position + full author, which blind
    matching rejects as 'extra tokens' but series-context accepts — while still rejecting the wrong
    volume or wrong series."""
    prefs = _prefs(preferred_formats=["epub"], languages=["en"])
    ctx = {"series": "Spellmonger", "author_full": "T. L. Mancour", "allow_volume": True}
    rel = FakeRelease("Mancour, Terry - Spellmonger 02 - Warmage [epub]")
    assert not rm.score_release("Warmage", "T. L. Mancour", "en", rel, prefs).auto_ok  # blind: strict
    assert rm.score_release("Warmage", "T. L. Mancour", "en", rel, prefs, context=ctx).auto_ok
    # wrong volume's release (Magelord) must NOT satisfy a request for Warmage
    mage = FakeRelease("Mancour, Terry - Spellmonger 03 - Magelord [epub]")
    assert not rm.score_release("Warmage", "T. L. Mancour", "en", mage, prefs, context=ctx).auto_ok
    # a different 'Warmage' book that doesn't name the series is rejected
    other = FakeRelease("Someone.Else-Warmage.A.Different.Book.epub")
    assert not rm.score_release("Warmage", "T. L. Mancour", "en", other, prefs, context=ctx).auto_ok
    # comma "Last, First" author now tokenizes correctly (surname matches)
    assert rm.title_author_confidence(
        "Warmage", "T. L. Mancour", rm.parse_release("Mancour, Terry - Warmage [epub]")) >= 0.95


def test_query_variants_multiple_naming_conventions():
    vs = rm.query_variants("Project Hail Mary", "Andy Weir")
    assert "project hail mary weir" in vs        # canonical (title + surname)
    assert "project hail mary andy weir" in vs   # full author
    assert "project hail mary" in vs             # title only
    assert len(vs) == len(set(v.lower() for v in vs))  # de-duplicated

    # Subtitle-stripped variant for "Title: Subtitle".
    sub = rm.query_variants("Dune: The Graphic Novel", "Frank Herbert")
    assert any(v == "dune herbert" for v in sub)
    assert any(v == "dune" for v in sub)

    # "Last, First" author renders to "First Last" in the full-author variant.
    comma = rm.query_variants("Warmage", "Mancour, Terry")
    assert "warmage terry mancour" in comma

    # Series context adds series+volume queries; ISBN passes through (13-digit kept).
    sc = rm.query_variants("Warmage", "Terry Mancour",
                           context={"series": "Spellmonger", "volume": 2},
                           isbns=["978-0-7653-8011-9", "junk"])
    assert "spellmonger 02" in sc and "spellmonger 2" in sc
    assert "9780765380119" in sc and "junk" not in sc


def test_candidate_dicts_orders_auto_then_speculative():
    prefs = _prefs(preferred_formats=["epub"], auto_grab_min_confidence=0.8)
    rels = [
        FakeRelease("Andy.Weir-Project.Hail.Mary.Retail.EPUB"),                 # auto_ok
        FakeRelease("Project.Hail.Mary.EPUB"),                                  # accepted, no author → spec
        FakeRelease("Andy.Weir-Project.Hail.Mary.Omnibus.Collection.EPUB"),    # boxset → multi, spec
    ]
    ranked = rm.rank_releases("Project Hail Mary", "Andy Weir", "en", rels, prefs)
    cands = rm.candidate_dicts(ranked, cap=6)
    assert cands[0]["auto_ok"] is True                  # auto candidate first
    assert any(c["is_multi"] for c in cands)            # boxset surfaced as a multi candidate
    assert all(c["download_url"] for c in cands)        # only releases with a URL
    assert all("key" in c for c in cands)               # carries a stable identity for broken-tracking
    # No-URL releases are excluded entirely.
    no_url = rm.candidate_dicts(
        rm.rank_releases("Project Hail Mary", "Andy Weir", "en",
                         [FakeRelease("Andy.Weir-Project.Hail.Mary.EPUB", download_url=None)], prefs))
    assert no_url == []


def test_release_key_prefers_guid_then_url():
    from app.ingestion.broken import release_key

    @dataclass
    class R:
        guid: str | None = None
        download_url: str | None = None

    assert release_key(R(guid="abc")) == "guid:abc"
    assert release_key(R(download_url="http://x/n")).startswith("url:")
    assert release_key(R()) is None
    assert release_key({"guid": "g1"}) == "guid:g1"


def test_non_string_title_does_not_crash():
    prefs = _prefs_strict()
    ranked = rm.rank_releases("Project Hail Mary", "Andy Weir", "en",
                              [FakeRelease(12345), FakeRelease("Andy.Weir-Project.Hail.Mary.EPUB")],
                              prefs)
    assert any(s.info.fmt == "epub" for s in ranked)  # bad one skipped, good one survives
