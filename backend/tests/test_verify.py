"""Post-download content verification: read embedded metadata, score against the requested book."""
from __future__ import annotations

import io
import zipfile

from app.ingestion import verify


def _make_epub(path, *, title, author):
    """A minimal but valid-enough EPUB (container + OPF) for metadata reading."""
    container = (
        '<?xml version="1.0"?><container version="1.0" '
        'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
        '<rootfiles><rootfile full-path="OEBPS/content.opf" '
        'media-type="application/oebps-package+xml"/></rootfiles></container>'
    )
    opf = (
        '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
        f'<dc:title>{title}</dc:title><dc:creator>{author}</dc:creator>'
        '<dc:language>en</dc:language></metadata></package>'
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip")
        zf.writestr("META-INF/container.xml", container)
        zf.writestr("OEBPS/content.opf", opf)
    path.write_bytes(buf.getvalue())
    return str(path)


def test_verify_reads_embedded_metadata(tmp_path):
    fp = _make_epub(tmp_path / "whatever-filename.epub",
                    title="Project Hail Mary", author="Andy Weir")
    vr = verify.verify_file(fp, "Project Hail Mary", "Andy Weir")
    assert vr.ok and vr.confidence >= 0.9 and vr.fmt == "epub"
    assert vr.title == "Project Hail Mary" and vr.author == "Andy Weir"


def test_verify_rejects_wrong_book(tmp_path):
    # The release name lied — the file inside is actually a different book.
    fp = _make_epub(tmp_path / "claims-to-be-phm.epub",
                    title="The Martian", author="Andy Weir")
    vr = verify.verify_file(fp, "Project Hail Mary", "Andy Weir")
    assert not vr.ok and vr.confidence < 0.6


def test_verify_author_mismatch_penalized(tmp_path):
    # Same title, different author (e.g. a study guide / wrong edition).
    fp = _make_epub(tmp_path / "x.epub", title="Project Hail Mary", author="Some Imitator")
    vr = verify.verify_file(fp, "Project Hail Mary", "Andy Weir")
    assert not vr.ok  # title matches but author disagrees → halved below the floor


def test_corrupt_download_falls_back_to_filename(tmp_path):
    # A non-EPUB / corrupt file: metadata unreadable, filename used as the title signal.
    fp = tmp_path / "Andy Weir - Project Hail Mary.epub"
    fp.write_bytes(b"not a real zip")
    vr = verify.verify_file(str(fp), "Project Hail Mary", "Andy Weir")
    assert vr.ok and vr.confidence >= 0.6  # filename contains the title


def test_verify_download_picks_best_file_in_dir(tmp_path):
    _make_epub(tmp_path / "extra.epub", title="Unrelated Companion", author="Nobody")
    _make_epub(tmp_path / "main.epub", title="Project Hail Mary", author="Andy Weir")
    vr = verify.verify_download(str(tmp_path), "Project Hail Mary", "Andy Weir")
    assert vr.ok and vr.title == "Project Hail Mary"


def test_match_titles_multi_book_pack(tmp_path):
    # A boxset directory with three volumes; map wanted series volumes to their files.
    _make_epub(tmp_path / "v1.epub", title="Spellmonger", author="Terry Mancour")
    _make_epub(tmp_path / "v2.epub", title="Warmage", author="Terry Mancour")
    _make_epub(tmp_path / "v3.epub", title="Magelord", author="Terry Mancour")
    wanted = [("a", "Warmage", "Terry Mancour"), ("b", "Magelord", "Terry Mancour"),
              ("c", "High Mage", "Terry Mancour")]  # not in the pack
    res = verify.match_titles(str(tmp_path), wanted)
    assert set(res) == {"a", "b"}                       # only the present volumes matched
    assert res["a"].title == "Warmage" and res["b"].title == "Magelord"
    assert res["a"].path != res["b"].path              # each file claimed once


def test_loose_containment_rejected(tmp_path):
    # A different, longer work whose title merely CONTAINS the requested phrase (a magazine), with no
    # matching author → must NOT verify (the loose-containment false positive found in live testing).
    fp = _make_epub(tmp_path / "x.epub",
                    title="Heated Rivalry: Inside TV's Hottest Show Spotlight 2026", author="")
    vr = verify.verify_file(fp, "Heated Rivalry", "Rachel Reid")
    assert not vr.ok and vr.confidence < 0.6
    # The genuine book (tight title, matching author) still verifies.
    ok = _make_epub(tmp_path / "y.epub", title="Heated Rivalry", author="Rachel Reid")
    assert verify.verify_file(str(ok), "Heated Rivalry", "Rachel Reid").ok


def test_no_book_file(tmp_path):
    (tmp_path / "readme.txt.nfo").write_text("scene release info")
    vr = verify.verify_download(str(tmp_path), "Project Hail Mary", "Andy Weir")
    assert not vr.ok and vr.path is None
