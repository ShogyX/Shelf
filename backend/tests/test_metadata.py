"""Metadata providers (ranobedb/goodreads) + match/enrich engine."""
from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from app.db import SessionLocal, init_db
from app.integrations import metadata as M
from app.integrations import metadata_sync as MS
from app.models import MetadataLink, Source, Work


class _Resp:
    def __init__(self, *, status=200, payload=None, text=""):
        self.status_code = status
        self._p = payload
        self.text = text or (json.dumps(payload) if payload is not None else "")

    def json(self):
        return self._p


def _fake_get(mapping):
    async def _get(self, url, **kw):
        for frag, resp in mapping.items():
            if frag in url:
                return resp
        return _Resp(status=404, payload={})
    return _get


SERIES_SEARCH = {"series": [{"id": 4239, "title": "Ascendance of a Bookworm",
                             "c_start_date": 20150201, "image": {"filename": "abc.jpg"}}]}
SERIES_DETAIL = {"series": {
    "id": 4239, "title": "Ascendance of a Bookworm", "description": "A girl reborn who loves books.",
    "publication_status": "completed",
    "staff": [{"role_type": "author", "name": "Miya Kazuki"}, {"role_type": "artist", "name": "X"}],
    "books": [{"id": 1, "c_release_date": 20150201, "image": {"filename": "cov.jpg"}},
              {"id": 2, "c_release_date": 20231209, "image": {"filename": "c2.jpg"}}],
    "child_series": [{"id": 6581, "title": "Ascendance of a Bookworm: Fanbook", "relation_type": "side story"}],
}}


def test_ranobedb_search_and_fetch(monkeypatch):
    p = M.RanobeDbProvider()
    monkeypatch.setattr(M.MetadataProvider, "_get",
                        _fake_get({"/series?q=": _Resp(payload=SERIES_SEARCH),
                                   "/series/4239": _Resp(payload=SERIES_DETAIL)}))
    import asyncio
    matches = asyncio.run(p.search("Ascendance of a Bookworm"))
    assert matches and matches[0].ref == "4239"
    assert matches[0].cover_url.endswith("/abc.jpg")
    meta = asyncio.run(p.fetch("4239"))
    assert meta.author == "Miya Kazuki"
    assert meta.synopsis.startswith("A girl reborn")
    assert meta.total_units == 2 and meta.unit_kind == "volumes"
    assert meta.status == "complete"
    assert meta.release_marker == "2:20231209"
    assert meta.related and meta.related[0].relation == "side story"
    assert meta.cover_url.endswith("/cov.jpg")


GR_RSS = """<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>Dungeon Crawler Carl (Dungeon Crawler Carl, #1)</title>
<author_name>Matt Dinniman</author_name><book_id>55781290</book_id>
<book_description>&lt;p&gt;A man and his cat.&lt;/p&gt;</book_description>
<book_large_image_url>https://img/cover.jpg</book_large_image_url></item>
</channel></rss>"""


def test_goodreads_wanted(monkeypatch):
    p = M.GoodreadsProvider(config={"user_id": "12345", "shelf": "to-read"})
    assert "list_rss/12345?shelf=to-read" in p._shelf_url()
    monkeypatch.setattr(M.MetadataProvider, "_get",
                        _fake_get({"list_rss": _Resp(text=GR_RSS)}))
    import asyncio
    wanted = asyncio.run(p.wanted())
    assert len(wanted) == 1
    assert wanted[0].title == "Dungeon Crawler Carl"   # series suffix stripped
    assert wanted[0].author == "Matt Dinniman"
    assert wanted[0].ref == "55781290"


def test_goodreads_user_id_required():
    with pytest.raises(M.IntegrationError):
        M.GoodreadsProvider(config={})._shelf_url()


def test_confidence_threshold():
    mk = lambda t, a=None: M.ProviderMatch(ref="1", title=t, author=a)
    assert MS._confidence("Ascendance of a Bookworm", None, mk("Ascendance of a Bookworm")) == 1.0
    # Different author known-disjoint lowers an exact-title score.
    assert MS._confidence("Re:Zero", "Tappei", mk("Re:Zero", "Other Author")) < 1.0
    # Unrelated short title doesn't match.
    assert MS._confidence("My Life", None, mk("My Next Life as a Villainess")) < MS.MATCH_THRESHOLD


def _png_bytes(color_fn):
    import io

    from PIL import Image
    im = Image.new("RGB", (64, 64))
    im.putdata([color_fn(i) for i in range(64 * 64)])
    buf = io.BytesIO(); im.save(buf, "PNG"); return buf.getvalue()


def test_gbooks_no_cover_placeholder_detection():
    from app import imagecache as IC
    # Mostly-white grayscale (the Google 'image not available' look) → detected.
    placeholder = _png_bytes(lambda i: (245, 245, 245) if i % 9 else (150, 150, 150))
    assert IC._is_gbooks_no_cover(placeholder) is True
    # A colourful real cover → never flagged.
    real = _png_bytes(lambda i: ((i * 7) % 256, (i * 13) % 256, (i * 29) % 256))
    assert IC._is_gbooks_no_cover(real) is False


def test_blank_cover_detection():
    import io

    from PIL import Image

    from app import imagecache as IC

    def _png(im):
        buf = io.BytesIO(); im.save(buf, "PNG"); return buf.getvalue()

    # 1×1 placeholder (Open Library's classic blank) → detected.
    assert IC._is_blank_cover(_png(Image.new("RGB", (1, 1), (255, 255, 255)))) is True
    # A flat single-colour fill at real dimensions → detected.
    assert IC._is_blank_cover(_png(Image.new("RGB", (180, 270), (12, 34, 56)))) is True
    # A colourful real cover → never flagged.
    real = Image.new("RGB", (64, 64))
    real.putdata([((i * 7) % 256, (i * 13) % 256, (i * 29) % 256) for i in range(64 * 64)])
    assert IC._is_blank_cover(_png(real)) is False
    # Host gate.
    assert IC._is_ol_cover_host("https://covers.openlibrary.org/b/isbn/123-M.jpg") is True
    assert IC._is_ol_cover_host("https://books.google.com/x.jpg") is False


class _ImgResp:
    def __init__(self, status, headers=None, content=b""):
        self.status_code = status; self.headers = headers or {}; self.content = content


def test_fetch_image_follows_safe_redirect(monkeypatch):
    """Open Library's cover CDN 302s to archive.org — _fetch_image must follow the redirect (with a
    fresh SSRF check on the hop) and return the real image, not reject it as a permanent failure."""
    from app import imagecache as IC
    img = _png_bytes(lambda i: ((i * 7) % 256, (i * 13) % 256, (i * 29) % 256))  # colourful, non-blank
    seq = {
        "https://covers.openlibrary.org/b/id/9-M.jpg": _ImgResp(302, {"location": "https://archive.org/x.jpg"}),
        "https://archive.org/x.jpg": _ImgResp(200, {"content-type": "image/png"}, img),
    }
    from urllib.parse import urlparse
    monkeypatch.setattr(IC, "assert_public_url", lambda u: [urlparse(u).hostname])  # offline; pin = no-op
    monkeypatch.setattr(IC, "_get_client", lambda: type("C", (), {"get": lambda s, u, headers=None, extensions=None: seq[str(u)]})())
    res = IC._fetch_image("https://covers.openlibrary.org/b/id/9-M.jpg", None)
    assert isinstance(res, tuple) and res[0] == img


def test_fetch_image_redirect_loop_is_capped(monkeypatch):
    from app import imagecache as IC
    from urllib.parse import urlparse
    monkeypatch.setattr(IC, "assert_public_url", lambda u: [urlparse(u).hostname])
    loop = _ImgResp(302, {"location": "https://a.test/next"})
    monkeypatch.setattr(IC, "_get_client", lambda: type("C", (), {"get": lambda s, u, headers=None, extensions=None: loop})())
    assert IC._fetch_image("https://a.test/start", None) == IC.PERMANENT_FAIL


def test_resolve_cover_falls_back_from_placeholder(monkeypatch):
    """When the full-res cover resolves to the rejected placeholder (PERMANENT_FAIL), _resolve_cover
    retries with the provider's plain thumbnail so a real (low-res) cover is still used."""
    from app import imagecache as IC
    hi = "https://books.google.com/books/content?id=x&img=1&zoom=0"
    thumb = "https://books.google.com/books/content?id=x&img=1&zoom=1"
    calls = []

    def fake_cache(url, **kw):
        calls.append(url)
        return IC.PERMANENT_FAIL if url == hi else "/media/imgcache/real.jpg"
    monkeypatch.setattr(IC, "cache_image", fake_cache)
    meta = M.ProviderMeta(ref="x", title="t", cover_url=hi, extra={"cover_thumb": thumb})
    import asyncio
    assert asyncio.run(MS._resolve_cover(meta)) == "/media/imgcache/real.jpg"
    assert calls == [hi, thumb]  # tried hi-res first, then the thumbnail fallback


def test_link_out_flags_chapter_discrepancy():
    """A metadata link reports the provider's max chapters and flags a >10 gap vs what we have."""
    from app.routers.metadata import _link_out

    chap_link = MetadataLink(id=1, work_id=1, provider="anilist", ref="x", confidence=1.0,
                             status="auto", total_units=200, unit_kind="chapters")
    out = _link_out(chap_link, known_chapters=150)
    assert out.expected_chapters == 200
    assert out.chapter_discrepancy == 50      # provider lists 50 more than we have
    assert out.major_discrepancy is True

    # A small gap (≤10) is not flagged.
    out2 = _link_out(chap_link, known_chapters=195)
    assert out2.expected_chapters == 200 and out2.major_discrepancy is False

    # A page/volume provider (Google Books) reports no chapter expectation, so nothing to flag.
    page_link = MetadataLink(id=2, work_id=1, provider="googlebooks", ref="y", confidence=1.0,
                             status="auto", total_units=320, unit_kind="pages")
    outp = _link_out(page_link, known_chapters=10)
    assert outp.expected_chapters is None and outp.major_discrepancy is False


def test_confidence_partial_overlap_needs_author_corroboration():
    """A web-novel ('Against the Gods') must NOT match a same-word-different book ('God Against
    the Gods' by Jonathan Kirsch) on title overlap alone — partial overlap needs author proof."""
    mk = lambda t, a=None: M.ProviderMatch(ref="1", title=t, author=a)
    # No author on our side → partial overlap is rejected outright.
    assert MS._confidence("Against the Gods", None, mk("God Against the Gods", "Jonathan Kirsch")) == 0.0
    # Conflicting known authors → still rejected.
    assert MS._confidence("Against the Gods", "Mars Gravity",
                          mk("God Against the Gods", "Jonathan Kirsch")) == 0.0
    # Same partial title WITH a corroborating author → allowed.
    assert MS._confidence("Against the Gods", "Jane Doe",
                          mk("Against the Gods Saga", "Jane Doe")) >= MS.MATCH_THRESHOLD


def test_confidence_subtitle_containment_clears_threshold():
    """A dropped subtitle makes the provider title a strict SUBSET of the work title (common for
    library/Gutenberg books). With corroborating authors that's a confident match — Jaccard alone
    would sink it below the threshold, so the containment boost must lift it over."""
    mk = lambda t, a=None: M.ProviderMatch(ref="1", title=t, author=a)
    score = MS._confidence("The Wives of Henry the Eighth and the Parts They Played in History",
                           "Martin Hume", mk("The Wives of Henry the Eighth", "Martin Hume"))
    assert score >= MS.MATCH_THRESHOLD
    # The boost is still author-gated: subset title with NO author corroboration is rejected.
    assert MS._confidence("The Wives of Henry the Eighth and the Parts They Played in History",
                          None, mk("The Wives of Henry the Eighth", None)) == 0.0


def test_meta_label_from_anilist_format():
    """AniList's format is mapped to the authoritative fine label used to override the heuristic."""
    mk = lambda fmt: M.ProviderMeta(ref="1", title="x", author=None, synopsis=None, cover_url=None,
                                    media_kind="comic", extra={"format": fmt})
    assert MS._meta_label(mk("MANGA")) == "Manga"
    assert MS._meta_label(mk("MANHWA")) == "Webtoon"
    assert MS._meta_label(mk("MANHUA")) == "Manhua"
    assert MS._meta_label(mk("NOVEL")) == "Novel"
    assert MS._meta_label(mk("TV")) is None        # anime format → no book label


def test_confidence_require_author_for_google_books():
    """require_author (Google Books) rejects even an EXACT title when we have no author to confirm
    it — the catalog is full of unrelated same-titled books."""
    mk = lambda t, a=None: M.ProviderMatch(ref="1", title=t, author=a)
    # No author on our side, exact title: blocked under require_author, allowed otherwise.
    assert MS._confidence("Perfect World", None, mk("Perfect World", "Karem Roitman"),
                          require_author=True) == 0.0
    assert MS._confidence("Perfect World", None, mk("Perfect World", "Karem Roitman")) == 1.0
    # Corroborating author satisfies require_author.
    assert MS._confidence("Perfect World", "Karem Roitman", mk("Perfect World", "Karem Roitman"),
                          require_author=True) == 1.0


class _FakeProvider(M.MetadataProvider):
    kind = "ranobedb"
    async def search(self, title, author=None, *, limit=8):
        return [M.ProviderMatch(ref="4239", title="Ascendance of a Bookworm")]
    async def fetch(self, ref):
        return M.ProviderMeta(ref="4239", title="Ascendance of a Bookworm", author="Miya Kazuki",
                              synopsis="Books.", total_units=33, status="complete",
                              release_marker="33:20231209")


def test_match_and_enrich_writes_link_and_metadata(monkeypatch):
    import app.imagecache as ic
    monkeypatch.setattr(ic, "cache_image", lambda u, **k: "/media/imgcache/x.jpg")
    init_db()
    db = SessionLocal()
    # get-or-create the shared 'generic_feed' Source (key is unique across the test DB)
    src = db.scalar(select(Source).where(Source.key == "generic_feed"))
    if src is None:
        src = Source(key="generic_feed", display_name="gf", adapter_key="generic_feed",
                     tos_permitted=True)
        db.add(src); db.commit()
    w = Work(source_id=src.id, source_work_ref="meta-test-r1", title="Ascendance of a Bookworm",
             hooked=True, status="ongoing", total_chapters_known=5)
    db.add(w); db.commit(); db.refresh(w)
    import asyncio
    link = asyncio.run(MS.match_and_enrich_work(db, w, _FakeProvider()))
    assert link is not None and link.provider == "ranobedb" and link.ref == "4239"
    db.refresh(w)
    assert w.author == "Miya Kazuki"
    assert w.description == "Books."
    # Volume count must NOT overwrite the chapter target (it lives on the link instead).
    assert w.total_chapters_expected is None
    assert link.total_units == 33 and link.unit_kind == "volumes"
    db.delete(link); db.delete(w); db.commit(); db.close()


def test_enrich_work_does_not_clobber_specialist_metadata():
    # MERGE-1: enrich_work must upgrade-not-clobber — a later broad provider (Google Books) must not
    # overwrite an existing specialist (RanobeDB) author/cover, and must keep the LONGER synopsis.
    init_db()
    db = SessionLocal()
    src = db.scalar(select(Source).where(Source.key == "generic_feed"))
    if src is None:
        src = Source(key="generic_feed", display_name="gf", adapter_key="generic_feed",
                     tos_permitted=True)
        db.add(src); db.commit()
    w = Work(source_id=src.id, source_work_ref="meta-merge1", title="Ascendance of a Bookworm",
             hooked=True, status="ongoing", author="Miya Kazuki",
             description="A long, rich specialist synopsis from RanobeDB.",
             cover_url="https://ranobedb/cover.jpg")
    db.add(w); db.commit(); db.refresh(w)
    broad = M.ProviderMeta(ref="gb1", title="Ascendance of a Bookworm", author="Some Editor",
                           synopsis="Short blurb.", cover_url="https://gbooks/cover.jpg")
    MS.enrich_work(db, w, broad)
    assert w.author == "Miya Kazuki"                       # existing author preserved
    assert w.cover_url == "https://ranobedb/cover.jpg"     # existing cover preserved
    assert w.description == "A long, rich specialist synopsis from RanobeDB."  # longer kept
    # A genuinely longer synopsis from a later provider still upgrades.
    longer = M.ProviderMeta(ref="gb1", title="Ascendance of a Bookworm",
                            synopsis="A long, rich specialist synopsis from RanobeDB, now extended.")
    MS.enrich_work(db, w, longer)
    assert w.description.endswith("now extended.")
    db.delete(w); db.commit(); db.close()


def test_provider_non_200_raises_not_empty(monkeypatch):
    """A non-200 (e.g. Google Books HTTP 429 quota) must RAISE, not masquerade as 0 results."""
    import asyncio
    p = M.GoogleBooksProvider()
    monkeypatch.setattr(M.MetadataProvider, "_get",
                        _fake_get({"/volumes": _Resp(status=429, text="quota exceeded")}))
    with pytest.raises(M.IntegrationError):
        asyncio.run(p.search("Tom Sawyer"))
    rp = M.RanobeDbProvider()
    monkeypatch.setattr(M.MetadataProvider, "_get",
                        _fake_get({"/series": _Resp(status=503, text="down")}))
    with pytest.raises(M.IntegrationError):
        asyncio.run(rp.search("anything"))


def test_enrich_library_aborts_and_surfaces_api_error(monkeypatch):
    """When the provider API fails, the sweep aborts and the error is reported (rather than
    scanning every work into the same wall and reporting a silent zero-match success)."""
    import asyncio
    init_db()
    db = SessionLocal()
    src = _src(db)
    w = Work(source_id=src.id, source_work_ref="meta-err1", title="Some Title", hooked=True)
    db.add(w); db.commit()

    class _DownProvider(M.MetadataProvider):
        kind = "ranobedb"
        tracks_releases = True
        async def search(self, title, author=None, *, limit=8):
            raise M.IntegrationError("ranobedb search HTTP 429: quota exceeded")

    summary = asyncio.run(MS.enrich_library(db, _DownProvider()))
    assert summary["matched"] == 0
    assert "429" in (summary["error"] or "")
    db.delete(w); db.commit(); db.close()


def _fake_post(search_payload, media_payload):
    async def _post(self, url, **kw):
        q = (kw.get("json") or {}).get("query", "")
        return _Resp(payload=media_payload if "Media(id" in q else search_payload)
    return _post


ANILIST_SEARCH = {"data": {"Page": {"media": [
    {"id": 105398, "format": "MANGA", "title": {"english": "Solo Leveling", "romaji": "Na Honjaman Level Up"},
     "coverImage": {"large": "https://img/sl.jpg"}, "startDate": {"year": 2018}, "siteUrl": "https://anilist.co/manga/105398"},
]}}}
ANILIST_MEDIA = {"data": {"Media": {
    "id": 105398, "format": "MANGA", "status": "FINISHED", "chapters": 201, "volumes": None,
    "title": {"english": "Solo Leveling"}, "description": "A hunter levels up. <b>Bold.</b>",
    "coverImage": {"extraLarge": "https://img/sl-xl.jpg", "large": "https://img/sl.jpg"},
    "siteUrl": "https://anilist.co/manga/105398",
    "staff": {"edges": [{"role": "Story", "node": {"name": {"full": "Chugong"}}},
                        {"role": "Art", "node": {"name": {"full": "DUBU"}}}]},
    "relations": {"edges": [
        {"relationType": "SEQUEL", "node": {"id": 179445, "type": "MANGA", "title": {"english": "Solo Leveling: Ragnarok"}}},
        {"relationType": "ADAPTATION", "node": {"id": 5, "type": "ANIME", "title": {"english": "Solo Leveling (anime)"}}},
    ]},
}}}


def test_anilist_search_and_fetch(monkeypatch):
    import asyncio
    p = M.AniListProvider()
    monkeypatch.setattr(M.MetadataProvider, "_post", _fake_post(ANILIST_SEARCH, ANILIST_MEDIA))
    matches = asyncio.run(p.search("Solo Leveling"))
    assert matches and matches[0].ref == "105398" and matches[0].media_kind == "comic"
    meta = asyncio.run(p.fetch("105398"))
    # The whole point: an authoritative CHAPTER count that can drive the source-of-truth.
    assert meta.total_units == 201 and meta.unit_kind == "chapters"
    assert meta.status == "complete" and meta.author == "Chugong"
    assert "Bold" in meta.synopsis and "<b>" not in meta.synopsis  # HTML stripped
    # Anime adaptation excluded; only the readable sequel is kept as a relation.
    assert [r.title for r in meta.related] == ["Solo Leveling: Ragnarok"]


def test_anilist_registered():
    assert "anilist" in M.METADATA_KINDS
    assert isinstance(M.provider_for(type("I", (), {"kind": "anilist", "base_url": "", "api_key": "", "config": {}})()),
                      M.AniListProvider)


def test_confidence_rejects_companion_edition():
    mk = lambda t: M.ProviderMatch(ref="x", title=t)
    # A fanbook/artbook the work title doesn't carry → not the same work → rejected outright.
    assert MS._confidence("Ascendance of a Bookworm", None, mk("Ascendance of a Bookworm: Fanbook")) == 0.0
    assert MS._confidence("Re:Zero", None, mk("Re:Zero Official Artbook")) == 0.0
    # But if the work IS the fanbook, the matching fanbook is allowed.
    assert MS._confidence("Bookworm Fanbook", None, mk("Bookworm Fanbook")) == 1.0


NU_SEARCH_HTML = """
<div class="search_main_box_nu"><div class="search_body_nu"><div class="search_title">
  <a href="https://www.novelupdates.com/series/reverend-insanity/">Reverend Insanity</a>
</div></div></div>
<div class="search_main_box_nu"><div class="search_body_nu"><div class="search_title">
  <a href="https://www.novelupdates.com/series/gu-daoist-master/">Gu Daoist Master</a>
</div></div></div>
"""

NU_SERIES_HTML = """
<html><body>
  <div class="seriesimg"><img src="https://cdn.novelupdates.com/img/ri.jpg"></div>
  <div class="seriestitlenu">Reverend Insanity</div>
  <div id="showauthors"><a href="#">Gu Zhen Ren</a></div>
  <div id="editstatus">2334 Chapters (Completed)</div>
  <div id="editdescription"><p>Humans are clever in tens of thousands of ways.</p></div>
</body></html>
"""


def test_novelupdates_parser():
    matches = M._nu_parse_search(NU_SEARCH_HTML, "https://www.novelupdates.com")
    assert [m.ref for m in matches] == ["reverend-insanity", "gu-daoist-master"]
    assert matches[0].title == "Reverend Insanity" and matches[0].media_kind == "text"

    meta = M._nu_parse_series(NU_SERIES_HTML, "reverend-insanity",
                              "https://www.novelupdates.com/series/reverend-insanity/")
    # The authoritative web-novel CHAPTER count (not volumes/pages) — drives the source of truth.
    assert meta.total_units == 2334 and meta.unit_kind == "chapters"
    assert meta.status == "complete" and meta.author == "Gu Zhen Ren"
    assert meta.media_kind == "text"
    assert "clever" in meta.synopsis
    assert M._nu_parse_series("<html><body>no series here</body></html>", "x", "u") is None


def test_novelupdates_challenge_raises(monkeypatch):
    import asyncio
    p = M.NovelUpdatesProvider(config={"cf_clearance": "stale-token"})
    # A returned Cloudflare interstitial must raise (never masquerade as 0 results).
    monkeypatch.setattr(M.MetadataProvider, "_get",
                        _fake_get({"novelupdates.com": _Resp(status=200,
                                  text="<title>Just a moment...</title>")}))
    with pytest.raises(M.IntegrationError):
        asyncio.run(p.search("anything"))


def test_novelupdates_registered():
    assert "novelupdates" in M.METADATA_KINDS


def test_confidence_rejects_cross_media_adaptation():
    # A prose web-novel must NOT match its comic adaptation as the same work — their chapter
    # counts differ (e.g. Reverend Insanity novel 2334 ch vs the manhua's ~96), so an exact
    # title match across media is pushed below the link threshold.
    comic = M.ProviderMatch(ref="x", title="Reverend Insanity", media_kind="comic")
    assert MS._confidence("Reverend Insanity", None, comic, "text") < MS.MATCH_THRESHOLD
    # Same medium (comic work ↔ comic match) is unaffected.
    assert MS._confidence("Reverend Insanity", None, comic, "comic") == 1.0
    # Unknown medium on either side → no penalty (back-compat).
    assert MS._confidence("Reverend Insanity", None, comic) == 1.0


def test_reconcile_skips_cross_media_count(monkeypatch):
    import asyncio
    import app.ingestion.tracker as T
    init_db()
    db = SessionLocal()
    src = _src(db)
    # A prose novel work; a comic provider reports a (smaller, unrelated) chapter count.
    w = Work(source_id=src.id, source_work_ref="meta-xm1", title="N", hooked=True,
             media_kind="text", total_chapters_known=10)
    db.add(w); db.commit()
    calls: list[int] = []
    monkeypatch.setattr(T, "check_work", lambda db, work: calls.append(work.id))
    comic_meta = M.ProviderMeta(ref="1", title="N", total_units=96, unit_kind="chapters",
                                media_kind="comic")
    assert asyncio.run(MS.reconcile_chapter_count(db, w, comic_meta)) is False and calls == []
    db.delete(w); db.commit(); db.close()


def test_reconcile_chapter_count_triggers_fetch_when_behind(monkeypatch):
    import asyncio
    import app.ingestion.tracker as T
    init_db()
    db = SessionLocal()
    src = _src(db)
    w = Work(source_id=src.id, source_work_ref="meta-rec1", title="W", hooked=True,
             total_chapters_known=100)
    db.add(w); db.commit()
    calls: list[int] = []
    async def _fake_check(db, work):
        calls.append(work.id); return {}
    monkeypatch.setattr(T, "check_work", _fake_check)

    # Provider reports MORE chapters than downloaded → raise target + trigger a source fetch.
    behind = M.ProviderMeta(ref="1", title="W", total_units=150, unit_kind="chapters")
    assert asyncio.run(MS.reconcile_chapter_count(db, w, behind)) is True
    db.refresh(w)
    assert w.total_chapters_expected == 150 and calls == [w.id]

    # Not behind (count <= downloaded) → no-op, no fetch.
    calls.clear()
    level = M.ProviderMeta(ref="1", title="W", total_units=80, unit_kind="chapters")
    assert asyncio.run(MS.reconcile_chapter_count(db, w, level)) is False and calls == []

    # A volume/page provider can't drive chapters → no-op even with a huge count.
    vols = M.ProviderMeta(ref="1", title="W", total_units=999, unit_kind="volumes")
    assert asyncio.run(MS.reconcile_chapter_count(db, w, vols)) is False and calls == []

    # trigger=False (on-hook path): raise the target but DON'T re-run source discovery.
    calls.clear()
    db.refresh(w)
    ahead = M.ProviderMeta(ref="1", title="W", total_units=300, unit_kind="chapters")
    assert asyncio.run(MS.reconcile_chapter_count(db, w, ahead, trigger=False)) is False
    db.refresh(w)
    assert w.total_chapters_expected == 300 and calls == []  # target raised, no fetch
    db.delete(w); db.commit(); db.close()


def test_anilist_fetch_many_batches_one_call(monkeypatch):
    """14B: fetch_many issues a SINGLE id_in batch query for many refs (not one per ref) and maps
    results back by id; a non-integer ref resolves to None."""
    import asyncio

    prov = M.AniListProvider()
    calls = {"n": 0}

    async def fake_gql(self, query, variables):
        calls["n"] += 1
        assert "id_in" in query and "Page" in query
        ids = variables["ids"]
        return {"Page": {"media": [
            {"id": i, "title": {"romaji": f"T{i}"}, "chapters": 10,
             "status": "RELEASING", "format": "MANGA"} for i in ids
        ]}}

    monkeypatch.setattr(M.AniListProvider, "_gql", fake_gql)
    out = asyncio.run(prov.fetch_many(["1", "2", "3", "bad"]))
    assert calls["n"] == 1                          # ONE batched round-trip for all valid ids
    assert out["1"].title == "T1" and out["2"].ref == "2"
    assert out["bad"] is None                       # non-int ref → None


def test_check_releases_does_not_recrawl_permanently_behind_work(monkeypatch):
    """A chapters provider whose count exceeds what the source can ever expose must NOT re-trigger
    a source crawl on every sweep — only when its marker actually advances."""
    import asyncio
    import app.ingestion.tracker as T
    from app.models import MetadataLink
    init_db()
    db = SessionLocal()
    src = _src(db)
    w = Work(source_id=src.id, source_work_ref="meta-pb1", title="Behind", hooked=True,
             media_kind="text", total_chapters_known=150)
    db.add(w); db.commit()
    link = MetadataLink(work_id=w.id, provider="anilist", ref="1", unit_kind="chapters",
                        total_units=201, release_marker="201:FINISHED")
    db.add(link); db.commit()
    calls: list[int] = []
    async def _fake_check(db, work):
        calls.append(work.id); return {}
    monkeypatch.setattr(T, "check_work", _fake_check)

    class _Stable(M.MetadataProvider):
        kind = "anilist"
        tracks_releases = True
        async def fetch(self, ref):  # same marker as the link → nothing advanced
            return M.ProviderMeta(ref="1", title="Behind", total_units=201, unit_kind="chapters",
                                  media_kind="text", release_marker="201:FINISHED")

    out = asyncio.run(MS.check_releases(db, _Stable()))
    assert out["checked"] == 1 and out["updated"] == 0
    assert calls == []  # source NOT re-crawled — marker unchanged despite known(150) < count(201)
    db.delete(link); db.delete(w); db.commit(); db.close()


# --------------------------------------------------------------- Pass 2: queue / hook
from app.models import CatalogWork, QueuedHook  # noqa: E402


def _src(db):
    src = db.scalar(select(Source).where(Source.key == "generic_feed"))
    if src is None:
        src = Source(key="generic_feed", display_name="gf", adapter_key="generic_feed",
                     tos_permitted=True)
        db.add(src); db.commit()
    return src


def test_queue_related_skips_owned_and_queued(monkeypatch):
    import asyncio

    from app.models import MetadataLink
    init_db()
    db = SessionLocal()
    src = _src(db)
    w = Work(source_id=src.id, source_work_ref="meta-q1", title="Mother Series",
             hooked=True, status="ongoing", author="A")
    db.add(w); db.commit(); db.refresh(w)
    link = MetadataLink(work_id=w.id, provider="ranobedb", ref="100",
                        payload={"related": [
                            {"title": "Sequel One", "relation": "sequel", "ref": "101"},
                            {"title": "Sequel One", "relation": "sequel", "ref": "101"},  # dup
                        ]})
    db.add(link); db.commit(); db.refresh(link)
    added = MS.queue_related(db, w, link)
    assert added == 1
    qh = db.scalar(select(QueuedHook).where(QueuedHook.reason == "related"))
    assert qh.title == "Sequel One" and qh.relation == "sequel" and qh.status == "pending"
    # Re-queuing is a no-op now that it's pending.
    assert MS.queue_related(db, w, link) == 0
    db.delete(qh); db.delete(link); db.delete(w); db.commit(); db.close()


def test_process_queued_hooks_hooks_when_in_index(monkeypatch):
    import asyncio

    init_db()
    db = SessionLocal()
    src = _src(db)
    # The title the operator queued.
    qh = QueuedHook(title="Found Title", norm_key="found title", reason="related",
                    media_kind="text", status="pending")
    db.add(qh)
    # A web_index catalog entry that just appeared for it.
    cw = CatalogWork(provider="web_index", domain="example.com",
                     work_url="https://example.com/found", norm_key="found title",
                     title="Found Title")
    db.add(cw); db.commit(); db.refresh(qh); db.refresh(cw)

    hooked_work = Work(source_id=src.id, source_work_ref="hooked-ft", title="Found Title",
                       hooked=True, status="ongoing")
    db.add(hooked_work); db.commit(); db.refresh(hooked_work)

    async def _fake_hook(_db, entry):
        entry.hooked_work_id = hooked_work.id
        return hooked_work
    import app.ingestion.catalog as cat
    monkeypatch.setattr(cat, "hook_entry", _fake_hook)

    res = asyncio.run(MS.process_queued_hooks(db))
    assert res["hooked"] == 1
    db.refresh(qh)
    assert qh.status == "hooked" and qh.hooked_work_id == hooked_work.id
    db.delete(qh); db.delete(cw); db.delete(hooked_work); db.commit(); db.close()


def test_process_queued_hooks_waits_when_not_indexed():
    import asyncio
    init_db()
    db = SessionLocal()
    qh = QueuedHook(title="Not Yet", norm_key="not yet here", reason="goodreads",
                    media_kind="text", status="pending")
    db.add(qh); db.commit(); db.refresh(qh)
    res = asyncio.run(MS.process_queued_hooks(db))
    assert res["hooked"] == 0
    db.refresh(qh)
    assert qh.status == "pending"  # still waiting for the index
    db.delete(qh); db.commit(); db.close()


def test_import_goodreads_queues_unowned(monkeypatch):
    import asyncio

    class _GR:
        kind = "goodreads"
        async def wanted(self):
            return [M.ProviderMatch(ref="1", title="Wishlist Book", author="Author X")]
    monkeypatch.setattr(M, "provider_for", lambda integ, config=None: _GR())

    init_db()
    db = SessionLocal()

    class _Integ:
        kind = "goodreads"
    res = asyncio.run(MS.import_goodreads(db, _Integ()))
    assert res["queued"] == 1
    qh = db.scalar(select(QueuedHook).where(QueuedHook.reason == "goodreads",
                                            QueuedHook.norm_key == "wishlist book"))
    assert qh is not None and qh.title == "Wishlist Book"
    db.delete(qh); db.commit(); db.close()


def test_check_releases_triggers_update_on_new_marker(monkeypatch):
    import asyncio

    from app.models import MetadataLink
    init_db()
    db = SessionLocal()
    for stale in db.scalars(select(MetadataLink).where(MetadataLink.provider == "ranobedb")).all():
        db.delete(stale)
    db.commit()
    src = _src(db)
    w = Work(source_id=src.id, source_work_ref="meta-rel1", title="Ongoing Work",
             hooked=True, status="ongoing")
    db.add(w); db.commit(); db.refresh(w)
    link = MetadataLink(work_id=w.id, provider="ranobedb", ref="555",
                        release_marker="3:20230101", total_units=3, unit_kind="volumes")
    db.add(link); db.commit()

    import app.imagecache as ic
    monkeypatch.setattr(ic, "cache_image", lambda u, **k: "/media/imgcache/x.jpg")

    calls = {"checked": 0}
    import app.ingestion.tracker as tracker
    async def _fake_check(_db, work):
        calls["checked"] += 1
    monkeypatch.setattr(tracker, "check_work", _fake_check)

    class _Prov(M.MetadataProvider):
        kind = "ranobedb"
        async def fetch(self, ref):
            return M.ProviderMeta(ref=ref, title="Ongoing Work", author="Au",
                                  total_units=4, unit_kind="volumes",
                                  release_marker="4:20240101", status="ongoing")
    res = asyncio.run(MS.check_releases(db, _Prov()))
    assert res["updated"] == 1 and calls["checked"] == 1
    db.refresh(link)
    assert link.release_marker == "4:20240101" and link.total_units == 4
    db.delete(link); db.delete(w); db.commit(); db.close()


def test_process_queued_hooks_gives_up_after_max_attempts(monkeypatch):
    import asyncio

    init_db()
    db = SessionLocal()
    qh = QueuedHook(title="Broken Hook", norm_key="broken hook key", reason="related",
                    media_kind="text", status="pending")
    cw = CatalogWork(provider="web_index", domain="example.com",
                     work_url="https://example.com/broken", norm_key="broken hook key",
                     title="Broken Hook")
    db.add(qh); db.add(cw); db.commit(); db.refresh(qh)

    import app.ingestion.catalog as cat
    async def _boom(_db, entry):
        raise RuntimeError("hook always fails")
    monkeypatch.setattr(cat, "hook_entry", _boom)

    for _ in range(MS.MAX_HOOK_ATTEMPTS):
        asyncio.run(MS.process_queued_hooks(db))
        db.refresh(qh)
    assert qh.attempts == MS.MAX_HOOK_ATTEMPTS
    assert qh.status == "failed"  # no longer retried / no longer starves the batch
    db.delete(qh); db.delete(cw); db.commit(); db.close()


# ----------------------------------------------------------------- Google Books
_GB_CONTENT = "http://books.google.com/books/content?id=vol-abc&printsec=frontcover&img=1"
GB_SEARCH = {"items": [
    {"id": "vol-abc", "volumeInfo": {
        "title": "The Beginning After the End", "authors": ["TurtleMe"],
        "publishedDate": "2018-05-01", "description": "King Grey reborn.",
        "categories": ["Fiction / Fantasy"],
        # Search results carry only the tiny thumbnail (zoom=1 ≈ 128px).
        "imageLinks": {"smallThumbnail": f"{_GB_CONTENT}&zoom=5&edge=curl",
                       "thumbnail": f"{_GB_CONTENT}&zoom=1&edge=curl"},
        "infoLink": "https://books.google.com/books?id=vol-abc"}},
    {"id": "vol-zzz", "volumeInfo": {"title": "Unrelated Cooking Manual", "authors": ["Someone"]}},
]}
GB_DETAIL = {"id": "vol-abc", "volumeInfo": {
    "title": "The Beginning After the End", "authors": ["TurtleMe"],
    "publishedDate": "2018-05-01", "description": "King Grey reborn into a world of magic.",
    "pageCount": 320, "categories": ["Comics & Graphic Novels"],
    "industryIdentifiers": [{"type": "ISBN_13", "identifier": "9781234567890"}],
    # The volume GET exposes the larger keys; we should prefer 'large' over 'thumbnail'.
    "imageLinks": {"thumbnail": f"{_GB_CONTENT}&zoom=1&edge=curl",
                   "large": f"{_GB_CONTENT}&zoom=4&edge=curl"},
    "infoLink": "https://books.google.com/books?id=vol-abc"}}


def test_googlebooks_search_and_fetch(monkeypatch):
    p = M.GoogleBooksProvider()
    assert p.base_url.endswith("/books/v1")
    monkeypatch.setattr(M.MetadataProvider, "_get",
                        _fake_get({"/volumes/vol-abc": _Resp(payload=GB_DETAIL),
                                   "/volumes?": _Resp(payload=GB_SEARCH),
                                   "/volumes": _Resp(payload=GB_SEARCH)}))
    import asyncio
    matches = asyncio.run(p.search("The Beginning After the End"))
    assert matches and matches[0].ref == "vol-abc"
    assert matches[0].author == "TurtleMe"
    assert matches[0].year == 2018
    # Cover upgraded to full resolution (zoom=0) with the page-curl overlay stripped + https.
    assert matches[0].cover_url == f"https://books.google.com/books/content?id=vol-abc&printsec=frontcover&img=1&zoom=0"
    meta = asyncio.run(p.fetch("vol-abc"))
    assert meta.title == "The Beginning After the End"
    assert meta.author == "TurtleMe"
    assert meta.synopsis.startswith("King Grey reborn into")
    assert meta.total_units == 320 and meta.unit_kind == "pages"
    assert meta.status == "complete"
    assert meta.media_kind == "comic"  # categorized as Comics & Graphic Novels
    # Prefers the larger 'large' key (not the 128px thumbnail) and forces full resolution.
    assert meta.cover_url == f"https://books.google.com/books/content?id=vol-abc&printsec=frontcover&img=1&zoom=0"


def test_googlebooks_api_key_is_sent(monkeypatch):
    p = M.GoogleBooksProvider(api_key="SECRET")
    seen = {}

    async def _get(self, url, **kw):
        seen["url"] = url
        return _Resp(payload=GB_SEARCH)
    monkeypatch.setattr(M.MetadataProvider, "_get", _get)
    import asyncio
    asyncio.run(p.search("anything"))
    assert "key=SECRET" in seen["url"]
    assert "printType=books" in seen["url"]


def test_googlebooks_registered():
    assert M.is_metadata_kind("googlebooks")
    assert isinstance(M.provider_for(
        type("I", (), {"kind": "googlebooks", "base_url": "", "api_key": "", "config": {}})()),
        M.GoogleBooksProvider)


def test_best_match_retries_with_cleaned_title(monkeypatch):
    """A messy crawl title that the provider's search can't satisfy verbatim is recovered by the
    cleaned-query fallback — and the match is still scored against the original title."""
    import asyncio
    queries: list[str] = []

    class _P(M.MetadataProvider):
        kind = "ranobedb"
        async def search(self, title, author=None, *, limit=8):
            queries.append(title)
            # Only return a usable candidate for the CLEANED query.
            if "chapter" in title.lower() or "|" in title:
                return []
            return [M.ProviderMatch(ref="9", title="Omniscient Reader's Viewpoint")]

    bm = asyncio.run(MS.best_match(_P(), "Omniscient Reader's Viewpoint - Chapter 12 | NovelSite", None))
    assert bm is not None and bm[1].ref == "9" and bm[0] >= MS.MATCH_THRESHOLD
    assert len(queries) == 2  # raw query missed, cleaned query hit
    assert "chapter" not in queries[1].lower()


def test_clean_query_strips_noise():
    assert MS._clean_query("Solo Leveling - Chapter 5 | AsuraScans").lower().strip() == "solo leveling"
    assert MS._clean_query("Mushoku Tensei, Vol. 12").lower().strip() == "mushoku tensei"
    assert MS._clean_query("Dungeon Crawler Carl (Dungeon Crawler Carl, #1)").strip() == "Dungeon Crawler Carl"


def test_create_googlebooks_integration_endpoint():
    """End-to-end: the API accepts the googlebooks kind (schema pattern) and reports it as a
    metadata provider. enabled=False skips the initial network sync."""
    from fastapi.testclient import TestClient
    from sqlalchemy import delete

    from app.main import app
    from app.models import Integration, User

    init_db()
    db = SessionLocal()
    db.execute(delete(Integration))
    db.execute(delete(User))
    db.commit()
    db.close()
    with TestClient(app) as c:
        c.post("/api/auth/setup", json={"username": "admin", "password": "test1234"})
        r = c.post("/api/integrations", json={"kind": "googlebooks", "enabled": False})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["kind"] == "googlebooks" and body["is_metadata"] is True
        assert body["has_api_key"] is False
