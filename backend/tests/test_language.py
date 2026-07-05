"""Language detection + canonicalization (release names + downloaded-file content)."""
from __future__ import annotations

from app.ingestion import language as lang


def test_canonicalize_codes_names_and_doublets():
    assert lang.canonicalize("en") == "en"
    assert lang.canonicalize("eng") == "en"
    assert lang.canonicalize("ger") == "de" and lang.canonicalize("deu") == "de"
    assert lang.canonicalize("fre") == "fr" and lang.canonicalize("fra") == "fr"
    assert lang.canonicalize("German") == "de"
    assert lang.canonicalize("en-US") == "en" and lang.canonicalize("pt_BR") == "pt"
    assert lang.canonicalize("xx") is None and lang.canonicalize("") is None


def test_detect_release_languages():
    assert lang.detect_languages("Andy Weir - Project Hail Mary German EPUB") == {"de"}
    assert lang.detect_languages("Author - Title FRENCH retail epub") == {"fr"}
    assert lang.detect_languages("Author - Title [ITA] epub") == {"it"}
    # No language stated.
    assert lang.detect_languages("Andy.Weir-Project.Hail.Mary.Retail.EPUB-GRP") == set()


def test_primary_language_prefers_trailing_tag():
    # A title word ("German") earlier, a real trailing tag wins (last occurrence).
    assert lang.primary_language("The German Wife - Author - English EPUB") == "en"
    assert lang.primary_language("Author - Title - German EPUB") == "de"
    assert lang.primary_language("Author - Title EPUB") is None


def test_case_sensitive_codes_and_sub_guard():
    assert "de" in lang.detect_languages("Author - Title DE epub")
    # Subtitle/codec contexts must NOT register as a spoken language.
    assert lang.detect_languages("Author - Title ES.SUB epub") == set()
    assert lang.detect_languages("Movie DTS-ES x264") == set()
    # lowercase 'es'/'de' inside words must not match the case-sensitive pass.
    assert lang.detect_languages("Desert Estate book") == set()


def test_two_letter_code_does_not_misflag_book_titles():
    # 'IT' is deliberately not a language code (too many books titled "It").
    assert lang.detect_languages("Stephen King - IT Retail EPUB") == set()
    # MULTi detected in parens / leading / trailing positions now.
    assert lang.is_multi_language("Author - Title (MULTI) epub") is True


def test_multi_language():
    assert lang.is_multi_language("Author - Title MULTi epub") is True
    assert lang.is_multi_language("Author - Title German English epub") is True
    assert lang.is_multi_language("Author - Title German epub") is False


def test_detect_text_language():
    en = "The quick brown fox was in the house and she had to go to the market with his dog " * 3
    de = "Der Mann ist nicht in dem Haus und das Kind hat eine Katze mit sich und auch der Hund " * 3
    assert lang.detect_text_language(en) == "en"
    assert lang.detect_text_language(de) == "de"
    assert lang.detect_text_language("too short") is None       # not enough tokens → no guess


def test_norwegian_support():
    # ISO-639-2 (nor) + bibliographic bokmål/nynorsk codes, English + native names all fold to "no".
    for tok in ("no", "nor", "nob", "nno", "Norwegian", "norsk", "bokmål", "nynorsk"):
        assert lang.canonicalize(tok) == "no", tok
    # release-name tags
    assert lang.detect_languages("Jo Nesbø - Snømannen (Norwegian) (epub)") == {"no"}
    assert lang.detect_languages("Jo Nesbo - Doktor Proktor NOB epub") == {"no"}
    assert lang.primary_language("Author - Tittel - Norsk EPUB") == "no"
    # content-based detection of undeclared Norwegian text (separates NO from EN)
    no_text = ("Det var en gang en mann som ikke hadde noe å gjøre og jeg skulle være på gården "
               "og hun hadde sagt at meg og seg selv ikke kunne etter det ") * 3
    assert lang.detect_text_language(no_text) == "no"
