"""Folder audiobooks: the health verdict must use the probe manifest, not one sampled file.

check_media_file probes the alphabetically-first file and read_audio_meta the largest, so a folder
can be "fine" on both while most of it is broken — or isn't even one book. That is exactly how a
folder holding 13 different Wheel of Time books, 256 of its tracks zero-byte, sat at health='ok'.
The manifest that would have said so was already cached on the Work and nothing read it.
"""
from __future__ import annotations

from app.ingestion.scheduler import _audio_manifest_verdict


class _W:
    def __init__(self, meta):
        self.id, self.audio_meta = 1, meta


def _tracks(n, *, bad=0, dur=600.0):
    return [{"index": i, "duration_s": (0.0 if i < bad else dur),
             "codec": (None if i < bad else "mp3")} for i in range(n)]


def test_no_manifest_leaves_the_verdict_alone():
    assert _audio_manifest_verdict(_W(None)) is None
    assert _audio_manifest_verdict(_W({"tracks": []})) is None


def test_healthy_book_is_not_flagged():
    meta = {"tracks": _tracks(20), "total_duration_s": 20 * 600}
    assert _audio_manifest_verdict(_W(meta)) is None


def test_a_folder_of_several_books_is_a_mismatch():
    """The real case: 1,582 tracks / ~250h under one title."""
    meta = {"tracks": _tracks(1582), "total_duration_s": 250 * 3600}
    health, detail = _audio_manifest_verdict(_W(meta))
    assert health == "mismatch"
    assert "1582 tracks" in detail and "several books" in detail


def test_partial_damage_stays_ok_but_says_so():
    """Crucially NOT 'corrupt': that feeds the re-fetch path, which deletes and re-downloads the
    whole title — losing 19 good tracks to repair 1 is worse than the fault itself."""
    meta = {"tracks": _tracks(20, bad=1), "total_duration_s": 19 * 600}
    health, detail = _audio_manifest_verdict(_W(meta))
    assert health == "ok"
    assert "1 of 20" in detail and "unplayable" in detail


def test_a_book_with_nothing_playable_is_corrupt():
    meta = {"tracks": _tracks(8, bad=8), "total_duration_s": 0}
    health, detail = _audio_manifest_verdict(_W(meta))
    assert health == "corrupt"
    assert "none of the 8" in detail


def test_a_very_long_single_book_is_judged_by_hours_too():
    """250 hours is not one audiobook even when the track count looks ordinary."""
    meta = {"tracks": _tracks(50), "total_duration_s": 250 * 3600}
    assert _audio_manifest_verdict(_W(meta))[0] == "mismatch"


def test_narrator_is_not_just_the_author_copied_over():
    """`artist` on an audiobook rip is the narrator about as often as the author — verify's
    read_audio_meta reads that same tag AS the author. Taking it blindly made 62% of this library's
    audiobooks "narrated by" their own author (Words of Radiance by Brandon Sanderson)."""
    from app.routers.delivery import _narrator_from_tags

    def info(**tags):
        return {"format": {"tags": tags}}

    # artist == author → not a narrator, it's the author's name in a second field.
    assert _narrator_from_tags(info(artist="Brandon Sanderson"), "Brandon Sanderson") is None
    # ...including trivial formatting differences.
    assert _narrator_from_tags(info(artist="brandon  sanderson!"), "Brandon Sanderson") is None
    # A genuinely different artist IS the narrator.
    assert _narrator_from_tags(info(artist="Michael Kramer"), "Brandon Sanderson") == "Michael Kramer"
    # Explicit tags win outright, even when they equal the author.
    assert _narrator_from_tags(info(narrator="Kate Reading"), "Kate Reading") == "Kate Reading"
    assert _narrator_from_tags(info(composer="Kate Reading", artist="Brandon Sanderson"),
                               "Brandon Sanderson") == "Kate Reading"
    # No usable tag at all.
    assert _narrator_from_tags(info(), "Brandon Sanderson") is None
    # Unknown author → fall back to the old permissive behaviour rather than losing the value.
    assert _narrator_from_tags(info(artist="Michael Kramer"), None) == "Michael Kramer"
