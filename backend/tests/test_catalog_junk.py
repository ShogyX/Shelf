"""Secondary-work (junk) detection: study guides / summaries / unofficial fan products / fan-fiction
spin-offs are hidden; real titles that merely contain a trigger word are never hidden."""
from __future__ import annotations

from app.ingestion.catalog_junk import _demo, is_secondary_work


def test_junk_patterns_self_check():
    _demo()   # asserts the full junk/real corpus (raises on any miss or false positive)


def test_fanfic_spinoffs_hidden_real_titles_kept():
    # Self-labelled fanfic spin-offs (the tag shapes observed in the live catalog) are junk…
    for t in ("Warlock of The Magus World (FanFiction)", "A strange new life [Naruto FanFic]",
              "The Chosen Few (A Blue Lock Fanfic)", "I Will Touch the Skies – A Pokemon Fanfiction",
              "Baystar's Belief (Warriors Fan-fiction Comic)"):
        assert is_secondary_work(t), t
    # …but real licensed works whose TITLE contains a fanfic word are kept.
    for t in ("My Secret XXX Fanfic", "Trapped in a Fan Fiction With My Bias",
              "FanFic Sensitivity", "Fangirl, Vol. 1", "Fanfiction"):
        assert not is_secondary_work(t), t


def test_fanfic_recovered_from_crawl_quoted_title():
    """A crawl-truncated title hides the fanfic tag — the aggregator boilerplate quotes the FULL
    title, which recovers it. Free-text fanfic mentions in a real blurb must NOT trigger."""
    boiler = "Read 'Chaos and Order - A Multiverse Fanfic' Novel Online for Free, written by X."
    assert is_secondary_work("Chaos and Order", boiler)
    assert not is_secondary_work("Chaos and Order")                    # title alone: no signal
    # A published novel whose blurb merely MENTIONS fanfiction stays visible.
    blurb = "Anna Todd's After fanfiction racked up a billion reads online."
    assert not is_secondary_work("After", blurb)
    # A real webtoon whose story is about fanfic (synopsis mentions it freely) stays visible.
    assert not is_secondary_work(
        "I Stan the Prince",
        "Angela's fanfic became such a sensation that it reached the palace.")
