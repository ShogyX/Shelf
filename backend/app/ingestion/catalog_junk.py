"""Detect 'secondary' catalog entries — study guides, summaries, workbooks, conversation-starters,
key-takeaways, SparkNotes/Quicklets, parody outlines, unofficial fan products — that merely SHARE a
real work's title and otherwise clutter search / series with confusing near-duplicates.

PRECISION over recall: a false positive HIDES a real book, so every pattern is ANCHORED to a
distinctive multi-word phrase or a structural prefix — never a bare word like "analysis",
"companion", "guide" or "parody" that legitimately appears inside real titles ("The Illustrated Man",
"…Structural Analysis Researcher", "Heart of a Companion", "A Guide to the Good Life", "Analysis of
Beauty"). Genuine editions / bundles (omnibus, box set, complete works, anthology) are NOT secondary
works — those are the real thing and are left alone (the catalog folds them as editions elsewhere).
"""
from __future__ import annotations

import re

# Distinctive phrases that only ever appear in companion / study / commentary products.
_PHRASES = re.compile(
    r"(?i)("
    r"study\s+guide"
    r"|conversation\s+starters?"
    r"|key\s+takeaways?"
    r"|\bsparknotes?\b|\bcliffs?\s*notes?\b"
    r"|\bquicklet\b"
    r"|summary\s*(?:[,&]| and )\s*analysis"          # "Summary & Analysis", "Summary, Analysis"
    r"|analysis\s*(?:[,&]| and )\s*review"           # "Analysis & Review"
    r"|(?:practice\s+)?workbook\s+(?:based\s+on|for|to)\b"
    r"|trivia[- ]on[- ]books"
    r"|\bparody\s+outline\b"
    r")"
)
# Structural PREFIX: "Summary of / Study Guide for / Summary Study Guide <real title>".
_PREFIX = re.compile(
    r"^\s*(?:the\s+|a\s+)?"
    r"(?:summary\s+(?:of|study|and|&)|study\s+guide|conversation\s+starters?|"
    r"key\s+takeaways?|quicklet|sparknotes?|cliffs?notes?)\b[\s:|.\-]+\S",
    re.I,
)
# Unofficial spin-offs / doujin fan products riding a real title (anchored to "unofficial …
# guide/fanbook/companion/sequel/spin-off" or an explicit (doujinshi) tag — never a bare
# "unofficial").
_UNOFFICIAL = re.compile(
    r"\bunofficial\s+(?:guide|fan\s?book|companion|handbook|encyclopedia"
    r"|sequel|spin[- ]?off|prequel|continuation|adaptation)\b"
    r"|\(\s*unofficial[^)]*(?:guide|fan|book)[^)]*\)"
    r"|\bdoujinshi\b",          # fan-made unofficial comic — never a word in an official title
    re.I,
)

# Fan-fiction SPIN-OFFS that self-label as such — web-crawled derivative works riding a franchise
# ("Warlock of The Magus World (FanFiction)", "[Naruto FanFic]", "I Will Touch the Skies – A Pokemon
# Fanfiction", "The Noble Queen-A Shadow Slave Fanfic"). Anchored to the TAG SHAPES only — a
# bracketed/parenthesized fanfic marker or a "– A <franchise> Fanfic(tion)" tail — because "fanfic"
# as a plain title word belongs to REAL licensed works ("My Secret XXX Fanfic", "Trapped in a Fan
# Fiction With My Bias", "FanFic Sensitivity") and to published novels ABOUT fanfic (Fangirl,
# After);
# synopsis mentions are even less reliable, so the check stays title-only.
_FANFIC_TAG = re.compile(
    r"\([^)]*\bfan[- ]?fic(?:tion)?\b[^)]*\)"        # "(FanFiction)", "(Pokemon Fanfiction OC)"
    r"|\[[^\]]*\bfan[- ]?fic(?:tion)?\b[^\]]*\]"     # "[Naruto FanFic]"
    r"|[-–—:]\s*a\s+[^-–—:]{0,50}\bfan[- ]?fic(?:tion)?\s*$",  # "… – A Shadow Slave Fanfic"
    re.I,
)


# Trailing "… Summary" — the summary-mill format ("<Title> by <Author> Summary", "<Author>'s <Title>
# Summary", "… Book/Diary Summary"). Anchored to an attribution so a real collection that merely ends
# in "Summaries" (e.g. "SALT Summaries") is NOT hit.
_TRAILING_SUMMARY = re.compile(
    r"(?i)(?:\bby\s+\S+.*|'s\s+\S.*|\b(?:book|short\s+reads?|chapter|diary|plot)\s+)summary\s*$")


# Web-crawl aggregator boilerplate that QUOTES the full original title ("Read 'Chaos and Order - A
# Multiverse Fanfic' Novel Online for Free …"). Crawled sites often truncate the stored title at the
# first separator, hiding the fanfic tag — the quoted full title still carries it. Anchored to the
# "Read '…' … Online" shape so a real book's synopsis that merely quotes something is never touched.
_CRAWL_QUOTED_TITLE = re.compile(r"^\s*read\s+['‘’]([^'‘’]{3,200})['‘’].{0,80}\bonline\b", re.I)


def is_secondary_work(title: str | None, synopsis: str | None = None) -> bool:
    """True for a study guide / summary / workbook / conversation-starters / SparkNotes / Quicklet /
    parody-outline / unofficial fan product / self-labelled fan-fiction spin-off that merely rides a
    real work's title. Conservative: anchored phrases/prefixes/tag-shapes only, so a real book is
    never hidden. ``synopsis`` (optional) is used ONLY to recover a crawl-truncated title from
    the aggregator's "Read '<full title>' … Online" boilerplate — never free-text matching
    (published books ABOUT fanfiction routinely mention it in their blurbs)."""
    t = (title or "").strip()
    if not t:
        return False
    if (_PREFIX.match(t) or _PHRASES.search(t) or _UNOFFICIAL.search(t)
            or _TRAILING_SUMMARY.search(t) or _FANFIC_TAG.search(t)):
        return True
    if synopsis:
        m = _CRAWL_QUOTED_TITLE.match(synopsis)
        if m and _FANFIC_TAG.search(m.group(1)):
            return True
    return False


def _demo() -> None:
    """Self-check: the companion/junk set is hidden; real titles that merely contain a trigger word
    are NOT (the precision cases that broke naive substring matching)."""
    junk = [
        "Summary of Sapiens", "A Study Guide for Ken Follett's \"World Without End\"",
        "The Secret History: A Novel by Donna Tartt - Conversation Starters",
        "Sapiens: by Yuval Noah Harari | Key Takeaways, Analysis & Review",
        "Practice WorkBook Based on Sapiens - a Brief History of Humankind",
        "Quicklet on Kathryn Stockett's The Help", "SparkNotes--Pride and Prejudice, Jane Austen",
        "CliffNotes on One day in the Life of Ivan Denisovich", "A Parody Outline of History",
        "Touhou - Unofficial Gensokyo Guide Book (Doujinshi)",
        "Summary Study Guide Sapiens : a Brief History of Humankind",
        "The Body Keeps the Score … | Key Takeaways, Analysis & Review",
        "Never Go Back by Lee Child Summary", "John Green's Paper Towns Summary",
        "Yuval Noah Harari's Sapiens Summary", "The Nightingale by Kristin Hannah: Book Summary",
        # Self-labelled fan-fiction spin-offs (tag shapes) + unofficial continuations.
        "Warlock of The Magus World (FanFiction)", "A strange new life [Naruto FanFic]",
        "Into the Unown (Pokemon Fanfiction OC)", "Togetherness (LN Fanfic)",
        "Mya Hero Academia (My Hero Academia Fanfiction)",
        "I Will Touch the Skies – A Pokemon Fanfiction", "The Noble Queen-A Shadow Slave Fanfic",
        "One Piece: The Unofficial Sequel", "Naruto Unofficial Spin-off",
    ]
    real = [
        "The Illustrated Man", "An Isekai Adventure Tale of a Former Structural Analysis Researcher",
        "Heart of a Companion", "Notes of a Crocodile", "The Complete Works of Kate Chopin",
        "Goodnight Punpun Omnibus, Vol. 1", "Sapiens: A Brief History of Humankind",
        "Analysis of Beauty", "A Guide to the Good Life", "Immoral Parody", "Love Trivia",
        "Not Your Sidekick", "The Annotated Sherlock Holmes", "SALT Summaries", "In Summary",
        # Real licensed works whose TITLE merely contains a fanfic word — must never be hidden.
        "My Secret XXX Fanfic", "Trapped in a Fan Fiction With My Bias", "FanFic Sensitivity",
        "The Ultimate Fanfic", "Fangirl, Vol. 1", "After", "Fanfiction",
    ]
    for t in junk:
        assert is_secondary_work(t), f"MISSED junk: {t!r}"
    for t in real:
        assert not is_secondary_work(t), f"FALSE POSITIVE (would hide a real book): {t!r}"
    print("catalog_junk self-check ok")


if __name__ == "__main__":
    _demo()
