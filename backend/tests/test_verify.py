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


def _make_epub_lang(path, *, title, author, language):
    opf = (
        '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
        f'<dc:title>{title}</dc:title><dc:creator>{author}</dc:creator>'
        f'<dc:language>{language}</dc:language></metadata></package>'
    )
    container = (
        '<?xml version="1.0"?><container version="1.0" '
        'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
        '<rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
        '</rootfiles></container>'
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


def test_corrupt_epub_is_rejected_by_integrity(tmp_path):
    # A corrupt/truncated .epub must be REJECTED (integrity), even if the filename matches — so it's
    # removed + re-downloaded rather than imported as a broken book.
    fp = tmp_path / "Andy Weir - Project Hail Mary.epub"
    fp.write_bytes(b"not a real zip" + b"x" * 300)
    vr = verify.verify_file(str(fp), "Project Hail Mary", "Andy Weir")
    assert not vr.ok and "integrity" in vr.reason


def test_check_integrity(tmp_path):
    import io
    import zipfile
    good = tmp_path / "good.epub"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml", '<container><rootfiles><rootfile full-path="c.opf"/></rootfiles></container>')
        z.writestr("c.opf", "<package><metadata></metadata></package>")
        z.writestr("ch.xhtml", "<html><body>" + "text " * 100 + "</body></html>")
    good.write_bytes(buf.getvalue())
    assert verify.check_integrity(str(good))[0] is True
    bad = tmp_path / "bad.epub"
    bad.write_bytes(buf.getvalue()[:200] + b"\x00" * 100)        # truncated zip
    assert verify.check_integrity(str(bad))[0] is False
    mobi = tmp_path / "x.epub"
    mobi.write_bytes(b"BOOKMOBI" + b"\x00" * 400)                  # mobi/PDB mislabeled .epub
    assert verify.check_integrity(str(mobi))[0] is False


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
    # A different work whose title merely CONTAINS the requested phrase (a magazine), with a
    # DIFFERENT author → must NOT verify (the false positive found in live testing; the matcher also
    # rejects "magazine" releases upstream, and author-miss halves the score here).
    fp = _make_epub(tmp_path / "x.epub",
                    title="Heated Rivalry: Inside TV's Hottest Show Spotlight 2026",
                    author="TV Guide Press")
    vr = verify.verify_file(fp, "Heated Rivalry", "Rachel Reid")
    assert not vr.ok and vr.confidence < 0.6
    # The genuine book (tight title, matching author) still verifies.
    ok = _make_epub(tmp_path / "y.epub", title="Heated Rivalry", author="Rachel Reid")
    assert verify.verify_file(str(ok), "Heated Rivalry", "Rachel Reid").ok


def test_short_title_long_subtitle_with_author(tmp_path):
    # A short title whose file carries a long legitimate subtitle still verifies WHEN the author
    # confirms — but a different longer work that merely contains the word does not.
    fp = _make_epub(tmp_path / "h.epub",
                    title="The Hobbit, or There and Back Again", author="J.R.R. Tolkien")
    assert verify.verify_file(str(fp), "The Hobbit", "J.R.R. Tolkien").ok
    other = _make_epub(tmp_path / "i.epub", title="It Ends With Us: A Novel", author="Colleen Hoover")
    assert not verify.verify_file(str(other), "It", "Stephen King").ok


def test_series_name_as_title_does_not_match_other_volume(tmp_path):
    # Requesting book 1 "Spellmonger" (whose title == the series name) must NOT match a different
    # volume whose subtitle names the series ("Shadowmage: Book Nine Of The Spellmonger Series").
    wrong = _make_epub(tmp_path / "v9.epub",
                       title="Shadowmage: Book Nine Of The Spellmonger Series", author="Terry Mancour")
    assert not verify.verify_file(str(wrong), "Spellmonger", "Terry Mancour").ok
    # Nor an anthology that merely names the series.
    anth = _make_epub(tmp_path / "anth.epub",
                      title="The Road To Sevendor - A Spellmonger Anthology", author="Terry Mancour")
    assert not verify.verify_file(str(anth), "Spellmonger", "Terry Mancour").ok
    # The genuine book 1 (file leads with the series/title) still matches.
    right = _make_epub(tmp_path / "v1.epub",
                       title="Spellmonger: Book One Of The Spellmonger Series", author="Terry Mancour")
    assert verify.verify_file(str(right), "Spellmonger", "Terry Mancour").ok
    # A volume whose own title is requested still matches even though the series is in the subtitle.
    war = _make_epub(tmp_path / "v2.epub",
                     title="The Spellmonger Series: Book 02 - Warmage", author="Terry Mancour")
    assert verify.verify_file(str(war), "Warmage", "Terry Mancour").ok


def test_language_verification(tmp_path):
    # Right title+author but the file is in German → rejected when English is requested.
    de = tmp_path / "de.epub"
    _make_epub_lang(de, title="Project Hail Mary", author="Andy Weir", language="de")
    vr = verify.verify_file(str(de), "Project Hail Mary", "Andy Weir", want_language="en")
    assert not vr.ok and "language" in vr.reason
    # Correct language passes; B/T doublet 'ger' is canonicalized to 'de'.
    en = tmp_path / "en.epub"
    _make_epub_lang(en, title="Project Hail Mary", author="Andy Weir", language="en")
    assert verify.verify_file(str(en), "Project Hail Mary", "Andy Weir", want_language="en").ok
    ger = tmp_path / "ger.epub"
    _make_epub_lang(ger, title="X", author="Y", language="ger")
    assert verify.file_language(str(ger)) == "de"
    # No language requested → language never blocks.
    assert verify.verify_file(str(de), "Project Hail Mary", "Andy Weir").ok


def test_verify_download_prefers_correct_language(tmp_path):
    _make_epub_lang(tmp_path / "wrong.epub", title="Dune", author="Frank Herbert", language="de")
    _make_epub_lang(tmp_path / "right.epub", title="Dune", author="Frank Herbert", language="en")
    vr = verify.verify_download(str(tmp_path), "Dune", "Frank Herbert", want_language="en")
    assert vr.ok and vr.path.endswith("right.epub")


def test_no_book_file(tmp_path):
    (tmp_path / "readme.txt.nfo").write_text("scene release info")
    vr = verify.verify_download(str(tmp_path), "Project Hail Mary", "Andy Weir")
    assert not vr.ok and vr.path is None


# ---- score_candidate (pre-download hit gate; shares the score_match core) --------------------
def _wm(title="Project Hail Mary", author="Andy Weir", titles=None, bucket="prose", isbn=None):
    from app.ingestion.matchmeta import WorkMeta
    return WorkMeta(titles=titles or [title], author=author, language="en", bucket=bucket,
                    media_kind="text", raw={"isbn": isbn} if isbn else {})


def test_score_candidate_accepts_matching_hit():
    cs = verify.score_candidate(_wm(), "Project Hail Mary", "Andy Weir", cand_type="Book")
    assert cs.accept and cs.score >= 0.9


def test_score_candidate_uses_alt_titles():
    # The romaji alt-title matches a hit catalogued under the native title.
    meta = _wm(title="Attack on Titan", author=None,
               titles=["Attack on Titan", "Shingeki no Kyojin"], bucket="comic")
    cs = verify.score_candidate(meta, "Shingeki no Kyojin Vol 1", None, cand_type="Comic")
    assert cs.score >= 0.85


def test_score_candidate_graded_author_hit_over_unknown():
    # A hit whose author confirms scores higher than one with no author metadata at all. Use a hit
    # whose title doesn't already saturate at 1.0 (a missing word), so the +0.1 author bonus is visible.
    meta = _wm()
    hit = verify.score_candidate(meta, "Hail Mary", "Andy Weir").score
    none_ = verify.score_candidate(meta, "Hail Mary", None).score
    assert hit > none_  # fuzzy author HIT adds +0.1 over the author-unknown baseline


def test_score_candidate_type_mismatch_sinks_below_floor():
    # A journal article of the same title is penalised below the candidate floor by type_compat.
    meta = _wm(title="Jane Eyre", author="Charlotte Bronte", bucket="prose")
    book = verify.score_candidate(meta, "Jane Eyre", "Bronte, Charlotte", cand_type="Book")
    article = verify.score_candidate(meta, "Jane Eyre", "Bronte, Charlotte", cand_type="Journal Article")
    assert book.score > article.score
    assert article.accept is False and article.score < 0.5


def test_score_candidate_isbn_short_circuit_and_none_isbn_harmless():
    meta = _wm(isbn=["9780593135204"])
    # An exact ISBN match IS the book regardless of a garbled title.
    assert verify.score_candidate(meta, "????", None, cand_isbn="9780593135204").accept
    # A None cand_isbn is harmless (falls through to title/author scoring).
    assert verify.score_candidate(meta, "Project Hail Mary", "Andy Weir", cand_isbn=None).accept


def test_score_candidate_type_compat_applied_once():
    # prose-vs-comic is 0.4; a perfect title (~1.0) → ~0.4, not 0.4² — i.e. applied exactly once.
    meta = _wm(title="Dune", author="Frank Herbert", bucket="prose")
    cs = verify.score_candidate(meta, "Dune", "Frank Herbert", cand_type="Comic")
    assert 0.35 <= cs.score <= 0.45


def test_norm_isbn_handles_x_check_digit_and_junk():
    # A valid ISBN-10 ending in the 'X' check digit converts to its ISBN-13 form.
    assert verify._norm_isbn("043942089X") == "9780439420891"
    assert verify._norm_isbn("080442957X") == "9780804429573"
    # A 13-digit ISBN passes through; a 10-digit numeric one converts.
    assert verify._norm_isbn("9780439420891") == "9780439420891"
    # Regression: an 'X' anywhere in the first 9 chars is NOT a valid ISBN-10 — it must return ""
    # rather than raise ValueError("invalid literal for int()") and crash catalog_regroup_tick.
    assert verify._norm_isbn("12345X7890") == ""
    assert verify._norm_isbn("X234567890") == ""
    assert verify._norm_isbn("1X34567890") == ""
    # Non-ISBN inputs are empty, not exceptions.
    assert verify._norm_isbn(None) == ""
    assert verify._norm_isbn("not-an-isbn") == ""


def test_norm_isbn_rejects_placeholder_fillers():
    # Filler ISBNs (all-same-digit) that providers stamp on record with no real one must NOT become
    # an identity key — otherwise every unrelated work sharing the placeholder unions into one group.
    assert verify._norm_isbn("0000000000") == ""
    assert verify._norm_isbn("9780000000002") == ""
    assert verify._norm_isbn("1111111111") == ""
    assert verify._norm_isbn("9781111111113") == ""
    assert verify._norm_isbn("9782222222226") == ""
    # A real ISBN whose body is NOT all-same-digit still normalizes (the GS1 978/979 prefix and the
    # check digit are stripped before the all-same test, so they don't cause false positives).
    assert verify._norm_isbn("9780306406157") == "9780306406157"
    assert verify._norm_isbn("9791234567896") == "9791234567896"
