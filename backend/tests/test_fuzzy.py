"""Dependency-free fuzzy scorers (E2) + their conservative use in titles_match."""
from __future__ import annotations

from app.ingestion.extract import norm_title, titles_match
from app.ingestion.fuzzy import ratio, token_set_ratio, token_sort_ratio


def test_ratio_basics():
    assert ratio("abc", "abc") == 100.0
    assert ratio("", "") == 100.0
    assert ratio("abc", "") == 0.0
    assert 80 < ratio("colour", "color") < 100   # one-char variant scores high but not perfect


def test_token_sort_is_order_insensitive():
    assert token_sort_ratio("dragon ball", "ball dragon") == 100.0
    # an extra token drags token_sort DOWN (unlike token_set which tolerates subsets)
    assert token_sort_ratio("one piece", "one piece party") < 90
    assert token_set_ratio("one piece", "one piece party") == 100.0   # why we DON'T use set here


def test_titles_match_admits_char_variants_not_spinoffs():
    def m(a, b):
        return titles_match(norm_title(a), None, norm_title(b), None)
    # char-level spelling variants (most tokens identical) → merge
    assert m("The Color of Magic", "The Colour of Magic")
    # distinct works that merely share a prefix → stay separate
    assert not m("One Piece", "One Piece Party")
    assert not m("Naruto", "Naruto Shippuden")
    # short-title-contained-in-longer must NOT merge (the Jaccard-vs-containment guard)
    assert not m("My Life", "My Next Life as a Villainess")


def test_titles_match_author_gate_still_blocks():
    # identical fuzzy titles but KNOWN disjoint authors → never merge
    assert not titles_match(norm_title("Eclipse"), "Stephenie Meyer",
                            norm_title("Eclipse"), "John Banville")
