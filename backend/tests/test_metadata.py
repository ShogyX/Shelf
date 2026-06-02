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
    monkeypatch.setattr(M, "provider_for", lambda integ: _GR())

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

    class _Prov:
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
