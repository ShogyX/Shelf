"""On-demand format conversion for the ABS/Storyteller integrations (Phase 0).

Guarded by skipif: these exercise the real calibre/ffmpeg binaries, which aren't present in every
environment. They run on hosts where the integration's conversion is actually available.
"""
from __future__ import annotations

import os
import subprocess

import pytest

from app.ingestion import convert


@pytest.mark.skipif(not convert._has_calibre(), reason="calibre (ebook-convert) not installed")
def test_to_epub_from_txt(tmp_path):
    txt = tmp_path / "book.txt"
    txt.write_text("Chapter One\n\n" + ("Hello world. " * 200))
    out = convert.to_epub_from(str(txt), str(tmp_path / "book.epub"))
    assert out and convert._valid_epub(out)


def test_to_epub_from_epub_is_noop(tmp_path):
    # An EPUB source must NOT be re-converted (returns None → caller uses it directly).
    assert convert.to_epub_from(str(tmp_path / "x.epub"), str(tmp_path / "y.epub")) is None


@pytest.mark.skipif(not convert.has_ffmpeg(), reason="ffmpeg not installed")
def test_to_m4b_folds_multiple_mp3(tmp_path):
    srcs = []
    for i in (1, 2):
        p = tmp_path / f"ch{i}.mp3"
        subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                        "-c:a", "libmp3lame", str(p)], check=True, capture_output=True)
        srcs.append(str(p))
    out = convert.to_m4b(srcs, str(tmp_path / "out.m4b"))
    assert out and os.path.getsize(out) > 0
