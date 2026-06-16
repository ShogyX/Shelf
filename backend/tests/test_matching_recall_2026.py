"""Recall improvements (2026-06-16): fuzzy author matching, softened release-name author penalty,
ISBN confirmation, and alternate-title verification — without re-opening the wrong-book false
positives the strict gates guard against. See docs/MATCHING_FIX_PLAN_2026-06-16.md."""
from __future__ import annotations

import io
import zipfile

from app.ingestion import fuzzy, verify
from app.ingestion.release_matcher import parse_release, title_author_confidence


# ---------------------------------------------------------------- fuzzy author
def test_author_similarity_initials_and_order():
    assert fuzzy.author_similarity("J. Smith", "John Smith") >= 0.8        # shared surname
    assert fuzzy.author_similarity("Tolkien, J.R.R.", "J.R.R. Tolkien") >= 0.8  # order-free
    assert fuzzy.author_similarity("J.R.R. Tolkien", "John Ronald Reuel Tolkien") >= 0.8


def test_author_similarity_transliteration():
    assert fuzzy.author_similarity("Fyodor Dostoevsky", "Fyodor Dostoyevsky") >= 0.8
    # surname-only transliteration (no shared first name to carry it)
    assert fuzzy.author_similarity("Dostoevsky", "Dostoyevsky") >= 0.8


def test_author_similarity_unrelated_below_threshold():
    assert fuzzy.author_similarity("Jane Austen", "Stephen King") < 0.8
    assert fuzzy.author_similarity("", "Andy Weir") == 0.0          # absent → never a match


# --------------------------------------------- pre-download release_matcher conf
def test_perfect_title_author_absent_from_name_is_speculative_not_deadbanded():
    # Release name carries NO author (common for scene releases). A perfect title must stay a
    # tried+verified candidate (>= the 0.6 cascade/match floor) rather than the old 0.60 dead-band.
    info = parse_release("Taming the Fire (epub)")
    conf = title_author_confidence("Taming the Fire", "Anne Author", info)
    assert conf >= 0.65          # tried, not abandoned
    assert conf < 0.8            # still speculative — verify (not the name) makes the call


def test_fuzzy_author_in_release_name_counts_as_present():
    # A transliterated author in the release name ("Tolkein") still confirms the author.
    info = parse_release("Tolkein - The Hobbit (retail epub)")
    conf = title_author_confidence("The Hobbit", "J.R.R. Tolkien", info)
    assert conf >= 0.95          # author confirmed → no penalty


def test_single_token_title_still_requires_author():
    info = parse_release("Dune Messiah Collection epub")   # author absent
    assert title_author_confidence("Dune", "Frank Herbert", info) == 0.0


# ------------------------------------------------------- ISBN normalization/match
def test_isbn_10_13_equivalence():
    assert verify._norm_isbn("0-306-40615-2") == verify._norm_isbn("978-0-306-40615-7")
    assert verify._isbn_match(["0306406152"], "9780306406157")
    assert not verify._isbn_match(["9780306406157"], "9781234567897")
    assert not verify._isbn_match([], "9780306406157")     # nothing to match


# ---------------------------------------------------------------- verify score_match
def test_score_match_isbn_short_circuits_author_mismatch():
    # Wrong embedded author, but the ISBN proves it's the book → full confidence.
    score, reason = verify.score_match(
        "Project Hail Mary", "Andy Weir", "Proyecto Hail Mary", "Traductor Anónimo",
        want_isbns=["9780306406157"], got_isbn="0-306-40615-2")
    assert score == 1.0 and "isbn" in reason


def test_score_match_alternate_title_matches():
    # File titled with the native/romaji name; the English want fails, but an alt title matches.
    score, _ = verify.score_match(
        "Re:Zero Starting Life in Another World", "Tappei Nagatsuki",
        "Re:Zero kara Hajimeru Isekai Seikatsu", "Tappei Nagatsuki",
        want_titles=["Re:Zero kara Hajimeru Isekai Seikatsu"])
    assert score >= 0.9          # alt-title + author hit


def test_score_match_fuzzy_author_not_a_miss():
    score, reason = verify.score_match("The Hobbit", "J.R.R. Tolkien",
                                       "The Hobbit", "John Ronald Reuel Tolkien")
    assert "hit" in reason and score >= 0.9


def test_score_match_genuine_wrong_author_still_rejected():
    # No ISBN, no alias, genuinely different author → stays strict (the false-positive guard).
    score, _ = verify.score_match("Project Hail Mary", "Andy Weir",
                                  "Project Hail Mary", "Some Imitator")
    assert score < 0.6


# ---------------------------------------------------- verify_file end-to-end with ISBN
def _make_epub_isbn(path, *, title, author, isbn):
    opf = (
        '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
        f'<dc:title>{title}</dc:title><dc:creator>{author}</dc:creator>'
        '<dc:identifier id="uuid">urn:uuid:1234</dc:identifier>'
        f'<dc:identifier id="isbn">{isbn}</dc:identifier>'
        '<dc:language>en</dc:language></metadata></package>'
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


def test_verify_file_isbn_rescues_wrong_embedded_author(tmp_path):
    fp = _make_epub_isbn(tmp_path / "x.epub", title="Project Hail Mary",
                         author="Editorial Imitator", isbn="978-0-306-40615-7")
    vr = verify.verify_file(fp, "Project Hail Mary", "Andy Weir", want_isbns=["0306406152"])
    assert vr.ok and vr.confidence == 1.0
