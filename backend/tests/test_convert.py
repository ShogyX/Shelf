"""mobi/azw3 → epub conversion + its wiring into the matcher / libgen format filter."""
from __future__ import annotations

import io
import zipfile

from app.ingestion import convert


def _epub_bytes():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml", '<container><rootfiles><rootfile full-path="c.opf"/></rootfiles></container>')
        z.writestr("c.opf", "<package><metadata><dc:title>X</dc:title></metadata></package>")
    return buf.getvalue()


def test_available_and_convertible():
    # the lightweight `mobi` lib is installed → conversion is available
    assert convert.available() is True
    assert convert.can_convert("/x/book.azw3") is True
    assert convert.can_convert("/x/book.epub") is False   # already importable
    assert convert.can_convert("/x/book.pdf") is False


def test_to_epub_noop_for_non_convertible(tmp_path):
    p = tmp_path / "a.epub"; p.write_bytes(_epub_bytes())
    assert convert.to_epub(str(p)) is None                # not a Kindle format
    assert convert.ensure_epub(str(p)) == str(p)          # unchanged


def test_ensure_epub_converts(tmp_path, monkeypatch):
    src = tmp_path / "book.azw3"; src.write_bytes(b"BOOKMOBI" + b"\x00" * 500)
    out = tmp_path / "book.azw3.converted.epub"; out.write_bytes(_epub_bytes())
    monkeypatch.setattr(convert, "to_epub", lambda s: str(out))
    res = convert.ensure_epub(str(src))
    assert res == str(out) and zipfile.is_zipfile(res)


def test_matcher_accepts_azw3_when_converter_available(monkeypatch):
    from app.ingestion import release_matcher as rm
    monkeypatch.setattr(convert, "available", lambda: True)
    prefs = rm.search_prefs(None)   # preferred_formats = epub/pdf/...
    az = rm.score_release("Project Hail Mary", "Andy Weir", "en",
                          _Rel("Andy.Weir-Project.Hail.Mary.azw3"), prefs)
    assert az.accepted and az.info.fmt == "azw3"   # accepted because it converts to epub
    monkeypatch.setattr(convert, "available", lambda: False)
    az2 = rm.score_release("Project Hail Mary", "Andy Weir", "en",
                           _Rel("Andy.Weir-Project.Hail.Mary.azw3"), prefs)
    assert not az2.accepted and "azw3" in az2.reason   # rejected when no converter


class _Rel:
    def __init__(self, title): self.title = title; self.size = 9_000_000
    categories = [7000]; grabs = 0; download_url = "http://x/nzb"
