"""AniList only catalogs manga + light novels, so nothing it matches may land in the prose 'Book'
category — comics go to 'Manga & Comics', everything else (light novels / 'books') to 'Novel'."""
from app.ingestion.catalog import has_anilist_identity, media_category, media_label
from app.models import CatalogWork


def _row(**kw):
    base = dict(provider="googlebooks", domain="books.google.com", work_url="w",
                title="T", media_kind="text", norm_key="t", extra={})
    base.update(kw)
    return CatalogWork(**base)


def test_anilist_text_is_novel_never_book():
    # A Google Books prose row that AniList ALSO matched (enrich_ref handle) → light Novel, not Book.
    r = _row(title="Re:Zero", norm_key="rezero", extra={"enrich_ref": {"anilist": "21355"}})
    assert has_anilist_identity(r)
    assert media_label(r) == "Novel"
    assert media_category(media_label(r)) == "Novel"

    # identity_key form is detected too.
    r2 = _row(title="Overlord", norm_key="overlord", identity_key="anilist:97668", extra={})
    assert media_label(r2) == "Novel"

    # Same provider, NO AniList signal → stays Book (we only divert AniList content).
    assert media_label(_row(title="A Memoir", norm_key="amemoir")) == "Book"


def test_anilist_comic_lands_in_manga_category():
    # AniList MANGA enrichment flips media_kind to comic + stamps the fine label → Manga & Comics.
    r = _row(provider="web_index", domain="x", title="One Piece", media_kind="comic",
             norm_key="onepiece", extra={"meta_label": "Manga", "enrich_ref": {"anilist": "30013"}})
    assert media_label(r) == "Manga"
    assert media_category(media_label(r)) == "Manga & Comics"


def test_group_label_diverts_even_when_anilist_member_is_not_rep():
    from app.ingestion.catalog_groups import _group_label
    # The popularity rep is a plain Google Books member; a different member carries the AniList id.
    rep = _row(title="Mushoku Tensei", norm_key="mushokutensei", popularity=99.0)
    ani = _row(provider="web_index", domain="d", title="Mushoku Tensei", norm_key="mushokutensei",
               extra={"enrich_ref": {"anilist": "1"}})
    assert _group_label([rep, ani], rep) == "Novel"     # AniList member wins → Novel, never Book


if __name__ == "__main__":
    test_anilist_text_is_novel_never_book()
    test_anilist_comic_lands_in_manga_category()
    test_group_label_diverts_even_when_anilist_member_is_not_rep()
    print("ok")
