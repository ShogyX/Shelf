"""Audiobook matching + verification (Phase 1 backend core)."""
from __future__ import annotations

from dataclasses import dataclass, field

from app.ingestion import release_matcher as rm
from app.ingestion import verify


@dataclass
class FakeRelease:
    title: str
    size: int = 80_000_000
    categories: list = field(default_factory=lambda: [3030])
    grabs: int = 0
    download_url: str | None = "http://idx/nzb"


def test_bare_audio_word_is_not_an_audiobook():
    # "audio"/"read" are common title words — they must NOT classify an EBOOK as an audiobook.
    info = rm.parse_release("The Audio Engineer's Handbook EPUB")
    assert not info.is_audiobook and info.fmt == "epub"
    assert not rm.parse_release("Read By Starlight - A Novel EPUB").is_audiobook


def test_audiobook_hints_and_formats_detected():
    assert rm.parse_release("Andy Weir - Project Hail Mary (Unabridged) MP3").is_audiobook
    assert rm.parse_release("Andy Weir - Project Hail Mary [M4B]").is_audiobook
    assert rm.parse_release("Dune narrated by Scott Brick AAX").is_audiobook
    assert rm.parse_release("Dune Audiobook", categories=[3030]).is_audiobook


def test_audio_search_prefs():
    p = rm.search_prefs(None, media_kind="audio")
    assert p["want_audiobooks"] and not p["want_ebooks"] and p["is_audio"]
    assert 3030 in p["categories"]
    assert "m4b" in p["preferred_formats"]


def test_audio_search_rejects_ebook_and_accepts_audiobook():
    prefs = rm.search_prefs(None, media_kind="audio")
    # A genuine ebook (ebook category, no audio markers) that leaks into the audio search is rejected.
    ebook = rm.score_release("Project Hail Mary", "Andy Weir", "en",
                             FakeRelease("Andy.Weir-Project.Hail.Mary.EPUB", categories=[7020]), prefs)
    assert not ebook.accepted and "not an audiobook" in ebook.reason
    # The real audiobook is accepted, carrying the audio marker.
    audio = rm.score_release("Project Hail Mary", "Andy Weir", "en",
                             FakeRelease("Andy Weir - Project Hail Mary Unabridged M4B"), prefs)
    assert audio.accepted and audio.info.is_audiobook and audio.info.fmt == "audio"


def test_ebook_search_rejects_audiobook():
    prefs = rm.search_prefs(None)  # default prose: want_audiobooks False
    audio = rm.score_release("Project Hail Mary", "Andy Weir", "en",
                             FakeRelease("Andy Weir - Project Hail Mary Unabridged M4B",
                                         categories=[7000]), prefs)
    assert not audio.accepted and "audiobook not wanted" in audio.reason


def test_verify_audiobook(tmp_path):
    # Single m4b whose filename carries the title → verifies; its path is the file itself.
    f = tmp_path / "Project Hail Mary - Andy Weir.m4b"
    f.write_bytes(b"\x00" * 2048)
    vr = verify.verify_audiobook(str(tmp_path), "Project Hail Mary", "Andy Weir")
    assert vr.ok and vr.fmt == "audio" and vr.path == str(f)

    # No audio file → not ok.
    empty = tmp_path / "sub"
    empty.mkdir()
    assert not verify.verify_audiobook(str(empty), "Project Hail Mary", "Andy Weir").ok

    # Wrong title in the filenames → name backstop fails.
    w = tmp_path / "wrong"
    w.mkdir()
    (w / "Completely Different Thing.mp3").write_bytes(b"\x00" * 2048)
    assert not verify.verify_audiobook(str(w), "Project Hail Mary", "Andy Weir").ok
