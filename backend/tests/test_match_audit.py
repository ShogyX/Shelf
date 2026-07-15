"""Wrong-match detection (match_audit): the classifier grades suspects the way the 2026-07 full-pool
audit adjudicated them, and usable_correction refuses junk/mojibake/swapped tags."""
from __future__ import annotations

from app.ingestion.match_audit import _junk_tag, classify, usable_correction
from app.models import Work


def _w(title, author=None, series=None):
    w = Work(title=title, author=author, media_kind="audio")
    w.series = series
    return w


def test_classify_file_vs_work():
    p = lambda **kw: {"kind": "file_vs_work", "score": kw.pop("score", 0.0),
                      "authors_ok": kw.pop("authors_ok", False), **kw}
    # Different title AND author in real tags → wrong.
    assert classify(_w("Second Foundation", "Isaac Asimov"),
                    p(embedded_title="Foundation's Fear", embedded_author="Gregory Benford")) == "wrong"
    # Album tag = the series name → ok.
    assert classify(_w("Golden Son", "Pierce Brown", series="Red Rising"),
                    p(embedded_title="Red Rising", authors_ok=True)) == "ok"
    # Slug/compacted containment → ok ("Outlander1", concatenated LibriVox slugs).
    assert classify(_w("Outlander", "Diana Gabaldon"),
                    p(embedded_title="Outlander1", authors_ok=True)) == "ok"
    # Junk/label tags say nothing → review, never wrong.
    assert classify(_w("Little Women", "Louisa May Alcott"),
                    p(embedded_title="Radio Theatre", embedded_author="Focus on the Family")) == "review"
    # Same author, different title (series-tagged or wrong-book-same-author) → human call.
    assert classify(_w("Exit Strategy", "Martha Wells"),
                    p(embedded_title="All Systems Red", authors_ok=True)) == "review"


def test_classify_hooks():
    p = {"kind": "work_vs_hook", "score": 0.0, "authors_ok": False}
    assert classify(_w("Blood Rites", "Jim Butcher"), p) == "wrong"     # unrelated hooked row
    assert classify(_w("X", "A"), {"kind": "work_vs_hook", "score": 0.0, "authors_ok": True}) == "review"


def test_usable_correction_guards():
    assert usable_correction("The Stand", "Stephen King") == ("The Stand", "Stephen King")
    assert usable_correction("ÿþO", "ÿþK") is None                       # mojibake
    assert usable_correction("Written by Tom Clancy", "Balance of Power") is None  # swapped tags
    assert usable_correction("Radio Theatre", "Focus on the Family") is None      # label tag
    # Narrator prefix stripped; series-in-author dropped (kept title, no author).
    assert usable_correction("Dead Beat", "Read by James Marsters") == ("Dead Beat", "James Marsters")
    assert usable_correction("Escape", "Rain Storm (John Rain 03)") == ("Escape", None)


def test_junk_tag():
    for junk in ("Disc 04", "no Title", "Album", "https://example.com", "LibriVox Weekly Poetry",
                 "Created: 1/17/2008", "ÿþO", ""):
        assert _junk_tag(junk), junk
    for real in ("The Stand", "En osynlig", "DCI Logan 07"):
        assert not _junk_tag(real), real
