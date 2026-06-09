"""Discovery layer: catalog enrichment taxonomy → persisted grouping (CatalogGroup/Tag/Category)
→ the Index page's rows/browse endpoints."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.db import SessionLocal, init_db
from app.ingestion import comix_catalog
from app.ingestion.catalog_groups import regroup_catalog
from app.main import app
from app.models import (
    CatalogCategory,
    CatalogGroup,
    CatalogTag,
    CatalogWork,
    IndexedPage,
    IndexSite,
    User,
    UserSession,
)


@pytest.fixture(autouse=True)
def _clean():
    init_db()
    db = SessionLocal()
    for model in (CatalogTag, CatalogCategory, CatalogGroup, CatalogWork, IndexedPage, IndexSite,
                  UserSession, User):
        db.execute(delete(model))
    db.commit()
    db.close()
    yield


@pytest.fixture
def client_admin():
    """An authenticated admin TestClient (fresh instance → setup creates the first admin)."""
    with TestClient(app) as c:
        c.post("/api/auth/setup", json={"username": "admin", "password": "hunter2pw"})
        yield c


def _row(db, *, title, domain, media="comic", pop=0.0, genres=(), themes=(), hid=None, ch=None):
    extra = {}
    if genres:
        extra["genres"] = [{"slug": g.lower(), "label": g} for g in genres]
    if themes:
        extra["themes"] = [{"slug": t.lower().replace(" ", "-"), "label": t} for t in themes]
    if hid:
        extra["hid"] = hid
    r = CatalogWork(domain=domain, work_url=f"https://{domain}/t/{title.replace(' ', '-')}",
                    title=title, norm_key=title.lower(), media_kind=media, popularity=pop,
                    chapters_advertised=ch, enriched_at=None, extra=extra or None)
    db.add(r)
    return r


def test_comix_upsert_captures_popularity_rating_year():
    db = SessionLocal()
    site = IndexSite(root_url="https://comix.to/", domain="comix.to", status="active",
                     max_pages=10, max_depth=2)
    db.add(site)
    db.commit()
    item = {"type": "manga", "title": "Test Manga", "hid": "abcd",
            "url": "/title/abcd-test-manga", "followsTotal": 4242, "ratedAvg": 8.5,
            "ratedCount": 120, "year": 2019, "latestChapter": 50}
    assert comix_catalog.upsert_item(db, site, item) is True
    db.commit()
    row = db.query(CatalogWork).filter_by(title="Test Manga").one()
    assert row.popularity == 4242.0
    assert row.rating == 8.5 and row.rating_count == 120 and row.year == 2019
    db.close()


def test_regroup_builds_groups_tags_categories_and_dedupes_across_sources():
    db = SessionLocal()
    # Same logical work from two sources → ONE group; tags rolled up + deduped.
    _row(db, title="Solo Leveling", domain="comix.to", pop=9000, genres=("Action", "Fantasy"),
         themes=("Dungeon",), hid="x1")
    _row(db, title="Solo Leveling", domain="webtoons.com", pop=10, genres=("Action",))
    _row(db, title="Berserk", domain="comix.to", pop=3000, genres=("Action", "Horror"), hid="x2")
    # A novel with the same title as a comic must NOT merge (different media bucket).
    _row(db, title="Solo Leveling", domain="novellunar.com", media="text", pop=5, genres=("Action",))
    db.commit()

    out = regroup_catalog(db)
    assert out["groups"] == 3  # SL-comic, Berserk-comic, SL-novel

    # The two comic sources collapsed into one Solo Leveling group with both sources as members.
    sl = db.query(CatalogGroup).filter_by(title="Solo Leveling", media_bucket="comic").one()
    assert sl.member_count == 2
    members = db.query(CatalogWork).filter_by(group_id=sl.id).count()
    assert members == 2
    # Action appears once on the group even though two members carried it.
    action = db.query(CatalogTag).filter_by(group_id=sl.id, kind="genre", slug="action").count()
    assert action == 1
    assert db.query(CatalogTag).filter_by(group_id=sl.id, kind="theme", slug="dungeon").count() == 1

    # popularity_norm is a within-(source, bucket) percentile → comix's most-followed is 1.0.
    top = db.query(CatalogGroup).filter_by(media_bucket="comic").order_by(
        CatalogGroup.popularity_norm.desc()).first()
    assert top.title == "Solo Leveling" and top.popularity_norm == pytest.approx(1.0)

    # Category counts: Action spans both comic groups (generic comix.to titles → "Comic" category).
    action_cat = db.query(CatalogCategory).filter_by(kind="genre", slug="action",
                                                     media_label="Comic").one()
    assert action_cat.group_count == 2
    db.close()


def test_regroup_is_idempotent_and_skips_when_unchanged():
    db = SessionLocal()
    _row(db, title="One Piece", domain="comix.to", pop=5000, genres=("Action",), hid="y1")
    db.commit()
    assert regroup_catalog(db)["groups"] == 1
    # Nothing changed → the watermark check short-circuits the rebuild.
    assert regroup_catalog(db).get("skipped") is True
    db.close()


def test_rows_and_browse_endpoints(client_admin):
    db = SessionLocal()
    for i in range(12):
        _row(db, title=f"Fantasy Hit {i}", domain="comix.to", pop=1000 + i,
             genres=("Fantasy",), hid=f"f{i}")
    _row(db, title="Lonely Drama", domain="comix.to", pop=50, genres=("Drama",), hid="d1")
    db.commit()
    regroup_catalog(db)
    db.close()

    # The comic subtypes share one "Manga & Comics" section now.
    COMICS = "Manga & Comics"
    rows = client_admin.get("/api/catalog/rows", params={"media": COMICS}).json()
    assert all(r["media_category"] == COMICS for r in rows)
    kinds = {(r["kind"], r["slug"]) for r in rows}
    assert ("popular", "") in kinds
    # Fantasy has >= the row threshold (12) so it's a lane; Drama (1) is below it.
    assert ("genre", "fantasy") in kinds
    assert ("genre", "drama") not in kinds
    fantasy = next(r for r in rows if r["slug"] == "fantasy")
    assert fantasy["count"] == 12 and len(fantasy["items"]) > 0
    assert fantasy["items"][0]["sources"], "each row item carries hookable sources"

    cats = client_admin.get("/api/catalog/categories", params={"media": COMICS}).json()["categories"]
    assert any(c["slug"] == "fantasy" and c["count"] == 12 for c in cats)

    browse = client_admin.get(
        "/api/catalog/browse",
        params={"dimension": "genre", "value": "fantasy", "media": COMICS, "sort": "popularity"},
    ).json()
    assert len(browse) == 12
    # popularity sort → highest-followed first.
    assert browse[0]["title"] == "Fantasy Hit 11"


def test_admin_category_caps_restrict_user_index(client_admin):
    """Admin controls which media categories each user may view: a global default for normal users,
    overridable per-user. Admins are unrestricted. Enforced server-side across rows/facets/browse.
    The four comic subtypes are one 'Manga & Comics' category, on the same level as Novel/Book."""
    COMICS = "Manga & Comics"
    db = SessionLocal()
    # Comics (various subtypes → all roll up to "Manga & Comics") on a comic source, plus a separate
    # Novel source — to prove the cap gates a whole category and that subtypes are unified.
    for i in range(10):
        _row(db, title=f"Action Manga {i}", domain="comix.to", pop=70 - i, genres=("Action",), hid=f"ma{i}")
        _row(db, title=f"Romance Webtoon {i}", domain="webtoons.com", pop=60 - i, genres=("Romance",))
        _row(db, title=f"Epic Novel {i}", domain="ranobedb.org", media="text", pop=50 - i,
             genres=("Adventure",))
    db.commit()
    regroup_catalog(db)
    db.close()

    # Admin sees every category; the comic subtypes are merged into one section.
    admin_rows = client_admin.get("/api/catalog/rows").json()
    assert {r["media_category"] for r in admin_rows} == {COMICS, "Novel"}
    # The merged comics "Most Popular" lane mixes Manga + Webtoon subtypes, ranked together.
    comic_pop = next(r for r in admin_rows if r["media_category"] == COMICS and r["kind"] == "popular")
    assert {it["media_label"] for it in comic_pop["items"]} & {"Manga", "Webtoon"}

    # Default cap for normal users → comics only.
    assert client_admin.put("/api/users/category-default",
                            json={"categories": [COMICS]}).status_code == 200
    client_admin.post("/api/users",
                      json={"username": "reader", "password": "hunter2pw", "role": "user"})

    def _login():
        c = TestClient(app)
        c.post("/api/auth/login", json={"username": "reader", "password": "hunter2pw"})
        return c

    with _login() as cu:
        assert {r["media_category"] for r in cu.get("/api/catalog/rows").json()} == {COMICS}
        fac = cu.get("/api/catalog/facets").json()
        assert set(fac["media"]) <= {COMICS}
        # Source filter only offers sources carrying a viewable category: comix.to + webtoons.com
        # (both comics) stay; the novel source is hidden from a comics-only user.
        assert set(fac["domains"]) == {"comix.to", "webtoons.com"}
        cats = cu.get("/api/catalog/categories").json()["categories"]
        assert cats and {c["media_category"] for c in cats} == {COMICS}
        assert cu.get("/api/catalog/browse", params={"media": "Novel"}).json() == []  # disallowed

    # Per-user override beats the default → Novel only.
    uid = next(u["id"] for u in client_admin.get("/api/users").json() if u["username"] == "reader")
    assert client_admin.patch(f"/api/users/{uid}",
                              json={"allowed_categories": ["Novel"]}).status_code == 200
    with _login() as cu:
        assert {r["media_category"] for r in cu.get("/api/catalog/rows").json()} == {"Novel"}

    # A legacy saved value (old fine comic label) still grants the merged category after the migration.
    assert client_admin.patch(f"/api/users/{uid}",
                              json={"allowed_categories": ["Webtoon"]}).status_code == 200
    with _login() as cu:
        assert {r["media_category"] for r in cu.get("/api/catalog/rows").json()} == {COMICS}

    # Reset to inherit (null) → back to the default (comics).
    assert client_admin.patch(f"/api/users/{uid}",
                              json={"allowed_categories": None}).status_code == 200
    with _login() as cu:
        assert {r["media_category"] for r in cu.get("/api/catalog/rows").json()} == {COMICS}


def test_taxonomy_adult_detection():
    """18+ detection = explicit-adult genres or a provider adult flag — NOT 'Mature'/'Ecchi'
    (often just dark/suggestive), and NOT theme strings like 'Adult Protagonist'."""
    from app.ingestion.catalog import is_adult_genre, taxonomy_is_adult
    assert is_adult_genre("Smut") and is_adult_genre("hentai") and is_adult_genre("Erotica")
    assert not is_adult_genre("Mature") and not is_adult_genre("Ecchi")
    assert taxonomy_is_adult({"genres": [{"slug": "smut", "label": "Smut"}]})
    assert taxonomy_is_adult({"adult": True})  # provider flag (AniList isAdult / GBooks MATURE)
    assert not taxonomy_is_adult({"genres": [{"slug": "action", "label": "Action"}]})
    # A theme named 'Adult Protagonist' must NOT mark the work 18+.
    assert not taxonomy_is_adult({"themes": [{"slug": "adult-protagonist", "label": "Adult Protagonist"}]})


def test_adult_gate_and_per_user_opt_in(client_admin):
    """18+ visibility = the admin gate ∩ the user's own per-category preference. Enabled by DEFAULT
    for everyone (gate = all, users inherit); each switch can be narrowed independently per category,
    and the gate bounds every user's preference."""
    COMICS = "Manga & Comics"
    db = SessionLocal()
    # Enough safe + adult titles per category to clear the row/browse thresholds.
    for i in range(10):
        _row(db, title=f"Clean Manga {i}", domain="comix.to", pop=90 - i, genres=("Action",), hid=f"cm{i}")
        a = _row(db, title=f"Lewd Manga {i}", domain="comix.to", pop=80 - i, genres=("Smut",), hid=f"lm{i}")
        a.is_adult = True
        _row(db, title=f"Clean Novel {i}", domain="ranobedb.org", media="text", pop=70 - i,
             genres=("Fantasy",))
        n = _row(db, title=f"Lewd Novel {i}", domain="ranobedb.org", media="text", pop=60 - i,
                 genres=("Smut",))
        n.is_adult = True
    db.commit()
    regroup_catalog(db)
    db.close()

    def _titles(rows):
        return {it["title"] for r in rows for it in r["items"]}

    # Default: nothing configured → the gate covers all categories and the admin inherits it, so
    # ALL 18+ content shows (comics AND novels), with the Smut lane present.
    ALL = {COMICS, "Novel", "Book"}
    me = client_admin.get("/api/auth/me").json()
    assert set(me["adult_allowed_categories"]) == ALL
    assert set(me["adult_categories"]) == ALL
    rows = client_admin.get("/api/catalog/rows").json()
    titles = _titles(rows)
    assert any(t.startswith("Lewd Manga") for t in titles)
    assert any(t.startswith("Lewd Novel") for t in titles)
    assert any(r["slug"] == "smut" for r in rows)
    browse = client_admin.get("/api/catalog/browse",
                              params={"dimension": "genre", "value": "smut", "media": COMICS}).json()
    assert browse and all(b["is_adult"] for b in browse)

    # Admin narrows the gate to comics only → adult novels disappear for everyone; comics stay.
    assert client_admin.put("/api/users/adult-allowed", json={"categories": [COMICS]}).status_code == 200
    assert client_admin.get("/api/users/adult-allowed").json()["categories"] == [COMICS]
    me = client_admin.get("/api/auth/me").json()
    assert set(me["adult_categories"]) == {COMICS}              # inherited, now bounded by the gate
    titles = _titles(client_admin.get("/api/catalog/rows").json())
    assert any(t.startswith("Lewd Manga") for t in titles)
    assert not any(t.startswith("Lewd Novel") for t in titles)
    cats = client_admin.get("/api/catalog/categories").json()["categories"]
    assert any(c["slug"] == "smut" and c["media_category"] == COMICS for c in cats)
    assert not any(c["slug"] == "smut" and c["media_category"] == "Novel" for c in cats)

    # The admin turns 18+ OFF for themselves (explicit empty preference) → no 18+ for them, even
    # though the gate is open.
    assert client_admin.put("/api/auth/me/adult", json={"categories": []}).status_code == 200
    assert client_admin.get("/api/auth/me").json()["adult_categories"] == []
    assert not any("Lewd" in t for t in _titles(client_admin.get("/api/catalog/rows").json()))

    # A brand-new user inherits the gate (comics) by default → sees adult comics out of the box.
    client_admin.post("/api/users", json={"username": "reader", "password": "hunter2pw", "role": "user"})

    def _login():
        c = TestClient(app)
        c.post("/api/auth/login", json={"username": "reader", "password": "hunter2pw"})
        return c

    with _login() as cu:
        assert set(cu.get("/api/auth/me").json()["adult_categories"]) == {COMICS}
        assert any(t.startswith("Lewd Manga") for t in _titles(cu.get("/api/catalog/rows").json()))
        # The user opts out of comics for themselves → 18+ gone for them only.
        assert cu.put("/api/auth/me/adult", json={"categories": []}).status_code == 200
        assert not any("Lewd" in t for t in _titles(cu.get("/api/catalog/rows").json()))

    # Admin disables the gate entirely → 18+ hidden for everyone regardless of preference.
    assert client_admin.put("/api/users/adult-allowed", json={"categories": []}).status_code == 200
    client_admin.post("/api/users", json={"username": "reader2", "password": "hunter2pw", "role": "user"})
    c2 = TestClient(app)
    c2.post("/api/auth/login", json={"username": "reader2", "password": "hunter2pw"})
    with c2:
        assert c2.get("/api/auth/me").json()["adult_categories"] == []
        assert not any("Lewd" in t for t in _titles(c2.get("/api/catalog/rows").json()))


@pytest.mark.asyncio
async def test_enrich_tick_does_not_stamp_on_transient_failure(monkeypatch):
    """A transient upstream failure (HTTP error) must NOT mark the row enriched (so it retries)
    and must stop the tick (so a failing API isn't hammered every row)."""
    from app.ingestion import catalog_enrichment as ce

    init_db()
    db = SessionLocal()
    site = IndexSite(root_url="https://comix.to/", domain="comix.to", status="active")
    db.add(site); db.commit()
    rows = [CatalogWork(site_id=site.id, work_url=f"https://comix.to/title/h{i}-x", domain="comix.to",
                        title=f"X{i}", media_kind="comic", popularity=float(100 - i),
                        extra={"hid": f"h{i}"}) for i in range(3)]
    db.add_all(rows); db.commit()

    calls = {"n": 0}
    async def _boom(client, db, row):
        calls["n"] += 1
        raise ce._Transient("comix HTTP 503")
    monkeypatch.setattr(ce, "_enrich_comix", _boom)

    out = await ce.enrich_catalog_tick(db, limit=3)
    # Backed off after the first transient failure — not all 3 hammered.
    assert calls["n"] == 1 and out["enriched"] == 0
    # No row was stamped → all remain eligible for a later retry.
    stamped = db.query(CatalogWork).filter(CatalogWork.enriched_at.isnot(None)).count()
    assert stamped == 0
    db.close()


@pytest.mark.asyncio
async def test_comix_enrich_prefers_anilist_popularity(monkeypatch):
    """Comic popularity must come from AniList's authoritative global count, not comix's
    manhwa-biased follow count; fall back to comix follows only when AniList has no match."""
    from app.ingestion import catalog_enrichment as ce

    init_db()
    db = SessionLocal()
    site = IndexSite(root_url="https://comix.to/", domain="comix.to", status="active")
    db.add(site); db.commit()
    row = CatalogWork(site_id=site.id, work_url="https://comix.to/title/pvry-one-piece",
                      domain="comix.to", title="One Piece", media_kind="comic", extra={"hid": "pvry"})
    db.add(row); db.commit()

    class _Resp:
        status_code = 200
        def json(self):
            return {"result": {"genres": [{"title": "Action"}], "tags": [], "demographics": [],
                               "type": "manga", "followsTotal": 20277, "ratedAvg": 8.6}}

    class _Client:
        async def get(self, url, headers=None):
            return _Resp()

    async def _pop(r):
        return 225208
    monkeypatch.setattr(ce, "_anilist_popularity", _pop)
    ok = await ce._enrich_comix(_Client(), db, row)
    assert ok and row.popularity == 225208.0  # AniList authority, NOT comix follows (20277)
    assert any(g.get("slug") == "action" for g in (row.extra or {}).get("genres", []))

    async def _none(r):
        return None
    monkeypatch.setattr(ce, "_anilist_popularity", _none)
    row.popularity = 0.0
    await ce._enrich_comix(_Client(), db, row)
    assert row.popularity == 20277.0  # no AniList match → comix follow count fallback
    db.close()


@pytest.mark.asyncio
async def test_backfill_comix_covers_fills_missing(monkeypatch):
    """A comix catalog row ingested WITHOUT a cover gets one backfilled from its poster (cover-only,
    no AniList) so it doesn't render coverless on the Index."""
    from app.ingestion import catalog_enrichment as ce
    from app.ingestion import netguard
    init_db()
    db = SessionLocal()
    db.execute(delete(CatalogWork)); db.commit()
    site = IndexSite(root_url="https://comix.to/", domain="comix.to", status="active")
    db.add(site); db.commit()
    row = CatalogWork(site_id=site.id, work_url="https://comix.to/title/pvry-pluto",
                      domain="comix.to", title="Pluto", media_kind="comic", popularity=100.0,
                      cover_url=None, extra={"hid": "pvry"})
    db.add(row); db.commit(); db.refresh(row)

    class _Resp:
        status_code = 200
        def json(self):
            return {"result": {"poster": {"large": "https://cdn.example/pluto.jpg"}}}

    class _Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, headers=None): return _Resp()

    monkeypatch.setattr(ce.httpx, "AsyncClient", lambda **k: _Client())
    monkeypatch.setattr(netguard, "assert_public_url", lambda url: None)
    out = await ce.backfill_comix_covers(db)
    db.refresh(row)
    assert out["filled"] == 1
    assert row.cover_url == "https://cdn.example/pluto.jpg"
    db.close()


@pytest.mark.asyncio
async def test_openlibrary_enriches_mainstream_book_popularity():
    """Mainstream prose books get a popularity signal (Open Library reading-log count) + genres,
    so future book titles rank on the same normalized scale as AniList-ranked manga. The loose
    Open Library search is gated by a title guard."""
    from app.ingestion import catalog_enrichment as ce

    init_db()
    db = SessionLocal()
    row = CatalogWork(work_url="test://book/dune", domain="examplebooks.test", title="Dune",
                      author="Frank Herbert", media_kind="text")
    db.add(row); db.commit()

    class _Resp:
        def __init__(self, payload): self._p = payload; self.status_code = 200
        def json(self): return self._p

    class _Client:
        def __init__(self, payload): self._p = payload
        async def get(self, url, headers=None): return _Resp(self._p)

    good = {"docs": [{"title": "Dune", "readinglog_count": 4215, "ratings_average": 4.3,
                      "subject": ["Science Fiction", "Dune (Imaginary place)", "Fiction"]}]}
    ok = await ce._enrich_openlibrary(_Client(good), db, row)
    assert ok and row.popularity == 4215.0 and row.enrich_source == "openlibrary"
    assert row.rating == 8.6  # OL's 0–5 normalized to the 0–10 convention
    slugs = [g["slug"] for g in (row.extra or {}).get("genres", [])]
    assert "science-fiction" in slugs and "fiction" not in slugs  # noise/place subjects dropped

    # Title guard: a wildly-different first hit is rejected (no popularity written).
    row2 = CatalogWork(work_url="test://book/x", domain="b.test", title="Dune", media_kind="text")
    db.add(row2); db.commit()
    wrong = {"docs": [{"title": "Completely Different Cookbook", "readinglog_count": 9999}]}
    assert await ce._enrich_openlibrary(_Client(wrong), db, row2) is False
    assert row2.popularity == 0.0
    db.close()


def test_popularity_norm_is_absolute_audience_not_percentile():
    """Ranking must reflect ABSOLUTE audience: un-enriched/obscure (raw 0) rows sit at 0 (never
    near the top), the single biggest hit is 1.0, and others scale ~linearly with audience — so
    obscure content can't surface in the top rows (the old percentile spread zero-ties up to 1.0)."""
    db = SessionLocal()
    _row(db, title="One Piece", domain="comix.to", pop=200000, hid="a")
    _row(db, title="Mid Hit", domain="comix.to", pop=50000, hid="b")
    # 12 un-enriched obscure comix rows (raw 0) — the percentile bug parked these up to 1.0.
    for i in range(12):
        _row(db, title=f"Obscure {i}", domain="comix.to", pop=0, hid=f"z{i}")
    db.commit()
    regroup_catalog(db)

    by_title = {g.title: g for g in db.query(CatalogGroup).all()}
    assert by_title["One Piece"].popularity_norm == pytest.approx(1.0)
    assert by_title["Mid Hit"].popularity_norm == pytest.approx(0.25, abs=0.02)  # 50k/200k
    # Every raw-0 group is pinned at 0 — nowhere near the top.
    assert all(by_title[f"Obscure {i}"].popularity_norm == 0.0 for i in range(12))
    db.close()


def test_book_providers_hidden_from_index_without_pipeline(client_admin):
    """googlebooks/openlibrary/hardcover items are only acquirable via the Prowlarr+SABnzbd
    pipeline → hidden from discovery when it isn't configured; Gutenberg (web_index) books and
    comics stay. Configuring the pipeline reveals them."""
    from app import cache
    from app.models import Integration
    db = SessionLocal()
    db.add(CatalogWork(provider="googlebooks", provider_ref="gb1", domain="books.google.com",
                       work_url="https://books.google.com/b/1", title="Mainstream Novel",
                       norm_key="mainstream novel", media_kind="text", popularity=5.0))
    db.add(CatalogWork(provider="web_index", domain="gutenberg.org",
                       work_url="https://gutenberg.org/ebooks/1", title="Classic Book",
                       norm_key="classic book", media_kind="text", popularity=5.0))
    db.add(CatalogWork(provider="web_index", domain="comix.to",
                       work_url="https://comix.to/t/x", title="Some Manga",
                       norm_key="some manga", media_kind="comic", popularity=5.0))
    db.commit()
    regroup_catalog(db)
    db.close()

    def row_titles():
        rows = client_admin.get("/api/catalog/rows").json()
        return {g["title"] for r in rows for g in r["items"]}

    cache.clear()
    titles = row_titles()
    assert "Classic Book" in titles and "Some Manga" in titles  # directly hookable → shown
    assert "Mainstream Novel" not in titles                     # pipeline-only book → hidden
    # The search grid hides it too.
    grid = {g["title"] for g in client_admin.get("/api/catalog?q=Mainstream").json()}
    assert "Mainstream Novel" not in grid
    # Facets are derived from the whole catalog (not a row sample): "Book" still shows (Gutenberg),
    # but the book-provider domain is dropped while no pipeline is configured.
    fac = client_admin.get("/api/catalog/facets").json()
    # Book = Gutenberg, still listed; the comic shows under the merged "Manga & Comics" category.
    assert "Book" in fac["media"] and "Manga & Comics" in fac["media"]
    assert "gutenberg.org" in fac["domains"] and "books.google.com" not in fac["domains"]

    # Configure the acquisition pipeline → the book item + its domain become visible.
    db = SessionLocal()
    db.add(Integration(kind="prowlarr", name="P", base_url="http://p", api_key="k", enabled=True))
    db.add(Integration(kind="sabnzbd", name="S", base_url="http://s", api_key="k", enabled=True))
    db.commit()
    db.close()
    cache.clear()
    assert "Mainstream Novel" in row_titles()
    assert "books.google.com" in client_admin.get("/api/catalog/facets").json()["domains"]
