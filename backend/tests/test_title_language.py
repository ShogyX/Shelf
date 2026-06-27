"""English/Latin canonical titles: the script heuristic, and that a group shows the English edition's
title even when a foreign-language member is more popular (Part 3 — 'one instance, shown in English')."""
from app.ingestion.extract import is_latin_title


def test_is_latin_title():
    assert is_latin_title("The Iliad")
    assert is_latin_title("Killing Floor")
    assert is_latin_title("Sich zum Henker")     # German is still Latin-script
    assert is_latin_title("")                     # nothing to prefer → treated as Latin
    assert not is_latin_title("Ἰλιάς")            # Greek
    assert not is_latin_title("Война и мир")      # Cyrillic
    assert not is_latin_title("鬼滅の刃")          # CJK


def test_group_rep_prefers_english_edition():
    from app.ingestion.catalog_groups import _build_groups
    from app.models import CatalogWork

    # Same work (shared ISBN identity), two editions. The Greek one is far more "popular" but the
    # display title must still be the English/Latin one.
    common = dict(provider="openlibrary", domain="d", media_kind="text", extra={},
                  identity_key="isbn:9780140275360")
    en = CatalogWork(id=1, provider_ref="a", work_url="w1", norm_key="iliad",
                     title="The Iliad", language="en", popularity=5.0, **common)
    gr = CatalogWork(id=2, provider_ref="b", work_url="w2", norm_key="ilias",
                     title="Ἰλιάς", language="grc", popularity=99.0, **common)
    groups = _build_groups([en, gr])
    assert len(groups) == 1                       # merged into one instance by shared identity
    assert groups[0]["title"] == "The Iliad"      # English wins despite lower popularity


if __name__ == "__main__":
    test_is_latin_title()
    test_group_rep_prefers_english_edition()
    print("ok")
