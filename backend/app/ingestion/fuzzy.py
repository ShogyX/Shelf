"""Small dependency-free fuzzy string scorers (RapidFuzz-shaped, 0-100), built on stdlib difflib.

Token-Jaccard alone (the existing matcher) misses transliteration / punctuation / plural / OCR
variants — "Re:Zero" vs "Re Zero", "Tolkien" vs "Tolkein", "Spider-Man" vs "Spiderman" — which are
the dominant manga/LN/book miss mode. These char-level ratios complement Jaccard: a high ratio with
a moderate token overlap is a genuine variant, not a coincidence. Kept dependency-free (no rapidfuzz)
to avoid a new pin; difflib's SequenceMatcher is plenty for short titles.
"""
from __future__ import annotations

from difflib import SequenceMatcher


def ratio(a: str, b: str) -> float:
    """Char-level similarity of two strings, 0-100 (RapidFuzz ``ratio`` convention)."""
    a = (a or "").strip()
    b = (b or "").strip()
    if not a and not b:
        return 100.0
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio() * 100.0


def token_sort_ratio(a: str, b: str) -> float:
    """``ratio`` after sorting each string's whitespace tokens — order-insensitive ("A B" == "B A")."""
    return ratio(" ".join(sorted((a or "").split())), " ".join(sorted((b or "").split())))


def token_set_ratio(a: str, b: str) -> float:
    """RapidFuzz-style token_set_ratio (0-100): compares the shared tokens against each string's
    remainder, so a title that's a superset of another (extra subtitle/qualifier tokens) still
    scores high. The max of three sub-comparisons, matching RapidFuzz's definition closely enough
    for title matching."""
    ta = sorted(set((a or "").split()))
    tb = sorted(set((b or "").split()))
    if not ta and not tb:
        return 100.0
    inter = sorted(set(ta) & set(tb))
    only_a = sorted(set(ta) - set(tb))
    only_b = sorted(set(tb) - set(ta))
    s_inter = " ".join(inter)
    s_a = (s_inter + " " + " ".join(only_a)).strip()
    s_b = (s_inter + " " + " ".join(only_b)).strip()
    # The intersection alone vs each combined string, and the two combined strings against each other.
    return max(
        ratio(s_inter, s_a) if s_inter else 0.0,
        ratio(s_inter, s_b) if s_inter else 0.0,
        ratio(s_a, s_b),
    )
