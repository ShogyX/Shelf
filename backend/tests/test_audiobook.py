"""Audiobook matching + verification (Phase 1 backend core)."""
from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field

import pytest

from app.ingestion import release_matcher as rm
from app.ingestion import verify

# The structural audio checks shell out to ffmpeg/ffprobe. Skip (don't fail) where the binaries
# aren't installed — CI installs them so the audio path is really exercised; a dev box without
# them still runs the rest of the suite.
_HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
requires_ffmpeg = pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg/ffprobe not installed")


def _real_audio(path, seconds: int = 31) -> None:
    """Write a real (silent) AAC audio file — verify_audiobook now structurally checks the audio
    (ffprobe + zero-prefix), so a fake byte blob no longer passes."""
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "anullsrc=r=22050:cl=mono",
         "-t", str(seconds), "-c:a", "aac", str(path)],
        check=True, capture_output=True, timeout=60)


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


@requires_ffmpeg
def test_verify_audiobook(tmp_path):
    # Single m4b whose filename carries the title → verifies; its path is the file itself.
    f = tmp_path / "Project Hail Mary - Andy Weir.m4b"
    _real_audio(f)
    vr = verify.verify_audiobook(str(tmp_path), "Project Hail Mary", "Andy Weir")
    assert vr.ok and vr.fmt == "audio" and vr.path == str(f)

    # No audio file → not ok.
    empty = tmp_path / "sub"
    empty.mkdir()
    assert not verify.verify_audiobook(str(empty), "Project Hail Mary", "Andy Weir").ok

    # Wrong title in the filenames → name backstop fails.
    w = tmp_path / "wrong"
    w.mkdir()
    _real_audio(w / "Completely Different Thing.m4b")
    assert not verify.verify_audiobook(str(w), "Project Hail Mary", "Andy Weir").ok


@requires_ffmpeg
def test_verify_audiobook_rejects_corrupt_audio(tmp_path):
    # A right-named file that ISN'T decodable audio (all-zero stub — a truncated/failed download)
    # must fail the structural backstop instead of importing and failing at playback.
    c = tmp_path / "corrupt"
    c.mkdir()
    (c / "Project Hail Mary - Andy Weir.m4b").write_bytes(b"\x00" * 2048)
    vr = verify.verify_audiobook(str(c), "Project Hail Mary", "Andy Weir")
    assert not vr.ok and "corrupt audio" in vr.reason


@requires_ffmpeg
def test_check_media_file(tmp_path):
    from app.ingestion.verify import check_media_file
    # Real audio passes; missing and zero-stub files fail with the corruption reasons.
    good = tmp_path / "book.m4b"
    _real_audio(good)
    assert check_media_file(str(good), "audio")[0]
    ok, why = check_media_file(str(tmp_path / "gone.m4b"), "audio")
    assert not ok and "missing" in why
    stub = tmp_path / "stub.m4b"
    stub.write_bytes(b"\x00" * 2048)
    ok, why = check_media_file(str(stub), "audio")
    assert not ok


@requires_ffmpeg
def test_verify_audiobook_tag_contradiction(tmp_path, monkeypatch):
    """A release whose FILENAMES carry the wanted title but whose TAGS identify a different book by
    a different author is rejected (the 'Second Foundation' file that was really Benford's
    "Foundation's Fear") — while series-name albums and junk/label tags never veto."""
    d = tmp_path / "dl"; d.mkdir()
    f = d / "Second Foundation - Isaac Asimov.m4b"
    _real_audio(f)
    # Different real book by a different author in the tags → rejected.
    monkeypatch.setattr(verify, "read_audio_meta",
                        lambda root: {"title": "Foundation's Fear", "author": "Gregory Benford"})
    vr = verify.verify_audiobook(str(d), "Second Foundation", "Isaac Asimov")
    assert not vr.ok and "tags contradict" in vr.reason
    # Series-name album by the SAME author → accepted (authors compatible).
    monkeypatch.setattr(verify, "read_audio_meta",
                        lambda root: {"title": "The Foundation Series", "author": "Isaac Asimov"})
    assert verify.verify_audiobook(str(d), "Second Foundation", "Isaac Asimov").ok
    # Junk/label tag → never a veto.
    monkeypatch.setattr(verify, "read_audio_meta",
                        lambda root: {"title": "Radio Theatre", "author": "Focus on the Family"})
    assert verify.verify_audiobook(str(d), "Second Foundation", "Isaac Asimov").ok
