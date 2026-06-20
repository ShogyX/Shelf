"""Open-library fallback pipeline: rate limiter, result parsing, candidate ranking, and the
download→verify→import worker (network fully mocked)."""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import delete, select

from app.db import SessionLocal, init_db
from app.ingestion import libgen as lg
from app.models import (
    CatalogWork,
    ContentRequest,
    DownloadJob,
    Integration,
    WorkSourceSearch,
)


@pytest.fixture
def db():
    init_db()
    s = SessionLocal()
    for m in (WorkSourceSearch, ContentRequest, DownloadJob, CatalogWork, Integration):
        s.execute(delete(m))
    s.commit()
    lg._HOSTS.clear()
    yield s
    s.close()


def _cfg(**over):
    base = dict(providers=["annas"], libgen_hosts=["libgen.la", "libgen.gl"], min_interval_s=0.0,
               max_per_day=1000, max_concurrent=2, formats=["epub", "pdf"], download_dir=None,
               annas_hosts=["annas-archive.gl"], annas_key=None)
    base.update(over)
    return lg.Config(**base)


def _cw(db, title="Pride and Prejudice", author="Jane Austen"):
    cw = CatalogWork(domain="d", work_url="u", title=title, author=author, norm_key=title.lower(),
                     media_kind="text")
    db.add(cw); db.commit(); db.refresh(cw)
    return cw


# ---- parsing / scoring ----------------------------------------------------------------------

def test_parse_size():
    assert lg._parse_size("388 kB") == 388_000
    assert lg._parse_size("1.5 MB") == 1_500_000
    assert lg._parse_size("nonsense") is None


def _meta(title="Pride and Prejudice", author="Jane Austen", titles=None, bucket="prose",
          media_kind="text"):
    from app.ingestion.matchmeta import WorkMeta
    return WorkMeta(titles=titles or [title], author=author, language="en", bucket=bucket,
                    media_kind=media_kind)


def test_candidates_filter_format_and_rank(db, monkeypatch):
    from app.ingestion import convert
    monkeypatch.setattr(convert, "available", lambda: False)   # no converter → mobi is dropped
    meta = _meta()
    hits = [
        lg.Hit("libgen", "Pride and Prejudice", "Jane Austen", "epub", 400_000, 2010, "en", "a"*32, "libgen.la", None, None),
        lg.Hit("libgen", "Pride and Prejudice", "Jane Austen", "mobi", 400_000, 2010, "en", "b"*32, "libgen.la", None, None),  # bad format
        lg.Hit("libgen", "Some Unrelated Title", "Other", "epub", 1, 2010, "en", "c"*32, "libgen.la", None, None),  # low score
    ]
    out = lg.candidates_for(meta, hits, _cfg())
    assert [h.md5 for h in out] == ["a"*32]   # mobi dropped (format), unrelated dropped (score)
    # with a converter available, the mobi candidate is accepted (converted to epub on download)
    monkeypatch.setattr(convert, "available", lambda: True)
    out2 = lg.candidates_for(meta, hits, _cfg())
    assert set(h.md5 for h in out2) == {"a"*32, "b"*32}


def test_score_hit_uses_alt_titles(db):
    # A manga catalogued under its English title still matches a hit under the romaji title, because
    # the romaji is one of the work's known titles.
    meta = _meta(title="Attack on Titan", author=None,
                 titles=["Attack on Titan", "Shingeki no Kyojin"], bucket="comic", media_kind="comic")
    h = lg.Hit("libgen", "Shingeki no Kyojin Vol 1", None, "cbz", 1, None, "ja", "a"*32, "libgen.la",
               None, None, content_type="Comic")
    assert lg._score_hit(meta, h) >= 0.85


def test_score_hit_penalizes_type_mismatch(db):
    # A journal article that merely mentions the title must sink below a real book of the same title.
    meta = _meta(title="Jane Eyre", author="Charlotte Bronte", bucket="prose")
    book = lg.Hit("libgen", "Jane Eyre", "Bronte, Charlotte", "epub", 1, 1847, "en", "a"*32,
                  "libgen.la", None, None, content_type="Book")
    article = lg.Hit("libgen", "Jane Eyre", "Bronte, Charlotte", "pdf", 1, 1980, "en", "b"*32,
                     "libgen.la", None, None, content_type="Journal Article")
    assert lg._score_hit(meta, book) > lg._score_hit(meta, article)
    assert lg._score_hit(meta, article) < 0.5   # article sinks below the candidate floor


def test_candidates_drops_boxset_and_companion(db):
    # A boxset/omnibus (wrong edition for a single title) and a companion product (study guide) are
    # dropped up front by the content gates, even though their titles contain the wanted words. Only
    # the plain single-volume hit survives.
    meta = _meta(title="Mistborn", author="Brandon Sanderson")
    hits = [
        lg.Hit("annas", "Mistborn", "Brandon Sanderson", "epub", 400_000, 2006, "en", "a"*32, None, None, None),
        lg.Hit("annas", "Mistborn: The Complete Boxset", "Brandon Sanderson", "epub", 9_000_000, 2010, "en", "b"*32, None, None, None),
        lg.Hit("annas", "Study Guide to Mistborn", "CliffsNotes", "epub", 100_000, 2015, "en", "c"*32, None, None, None),
    ]
    out = lg.candidates_for(meta, hits, _cfg())
    assert [h.md5 for h in out] == ["a"*32]


def test_candidates_keeps_boxset_when_request_is_a_bundle(db):
    # When the WORK itself was catalogued as a bundle ("Mistborn Omnibus"), a matching omnibus hit is
    # the right edition and must NOT be dropped by the boxset gate (mirrors release_matcher's intent).
    meta = _meta(title="Mistborn Omnibus", author="Brandon Sanderson")
    hits = [
        lg.Hit("annas", "Mistborn Omnibus", "Brandon Sanderson", "epub", 9_000_000, 2010, "en", "a"*32, None, None, None),
    ]
    out = lg.candidates_for(meta, hits, _cfg())
    assert [h.md5 for h in out] == ["a"*32]


def test_candidates_drops_wrong_language(db):
    # A hit that declares a different language than the requested one is the wrong edition → dropped.
    meta = _meta(title="Dune", author="Frank Herbert")          # language="en"
    hits = [
        lg.Hit("annas", "Dune", "Frank Herbert", "epub", 400_000, 1965, "English", "a"*32, None, None, None),
        lg.Hit("annas", "Dune", "Frank Herbert", "epub", 400_000, 1965, "German", "b"*32, None, None, None),
    ]
    out = lg.candidates_for(meta, hits, _cfg())
    assert [h.md5 for h in out] == ["a"*32]


SAMPLE_SEARCH = """
<table id="tablelibgen"><tr><th>x</th></tr>
<tr>
  <td><a href="edition.php?id=1">Pride and Prejudice</a></td>
  <td>Jane Austen</td><td>Pub</td><td>2010</td><td>English</td><td>0 / 5</td>
  <td><a href="/file.php?id=9">388 kB</a></td><td>epub</td>
  <td><a href="/ads.php?md5=AbCdef0123456789abcdef0123456789">1</a></td>
</tr></table>
"""


@pytest.mark.asyncio
async def test_libgen_search_parses_rows(db, monkeypatch):
    cw = _cw(db)
    f = lg.Fetcher(_cfg())

    async def fake_get_html(url, *, render=False, params=None):
        return SAMPLE_SEARCH
    monkeypatch.setattr(f, "get_html", fake_get_html)
    hits = await lg._libgen_search(f, _cfg(), cw)
    assert len(hits) == 1
    h = hits[0]
    assert h.md5 == "abcdef0123456789abcdef0123456789" and h.ext == "epub"
    assert h.title.startswith("Pride and Prejudice") and h.author == "Jane Austen"
    assert h.host == "libgen.la" and h.size == 388_000


@pytest.mark.asyncio
async def test_libgen_get_url_extracts_key(monkeypatch):
    f = lg.Fetcher(_cfg())
    ads_html = '<a href="get.php?md5=abcdef0123456789abcdef0123456789&key=XYZ123">GET</a>'

    async def fake_get_html(url, *, render=False, params=None):
        return ads_html
    monkeypatch.setattr(f, "get_html", fake_get_html)
    got = await lg._libgen_get_url(f, "libgen.la", "abcdef0123456789abcdef0123456789")
    assert got is not None
    url, referer = got
    assert url == "https://libgen.la/get.php?md5=abcdef0123456789abcdef0123456789&key=XYZ123"
    assert referer.endswith("ads.php?md5=abcdef0123456789abcdef0123456789")


@pytest.mark.asyncio
async def test_annas_search_parses_md5(db, monkeypatch):
    cw = _cw(db)
    f = lg.Fetcher(_cfg(providers=["annas"]))
    html = '<a href="/md5/dead00beef00dead00beef00dead00be">epub · en · 1.2MB · Pride and Prejudice · Austen</a>'

    async def fake_get_html(url, *, render=False, params=None):
        return html
    monkeypatch.setattr(f, "get_html", fake_get_html)
    hits = await lg._annas_search(f, _cfg(providers=["annas"]), cw)
    assert len(hits) == 1 and hits[0].md5 == "dead00beef00dead00beef00dead00be"
    assert hits[0].ext == "epub" and hits[0].provider == "annas"


# ---- rate limiter ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rate_limiter_daily_cap(db):
    cfg = _cfg(max_per_day=3, min_interval_s=0.0)
    for _ in range(3):
        await lg._throttle("libgen.la", cfg)
    with pytest.raises(lg.RateLimitExceeded):
        await lg._throttle("libgen.la", cfg)
    # a different host has its own budget
    await lg._throttle("libgen.gl", cfg)
    assert lg._HOSTS["libgen.la"].count == 3 and lg._HOSTS["libgen.gl"].count == 1


@pytest.mark.asyncio
async def test_rate_limiter_min_interval(db):
    cfg = _cfg(min_interval_s=0.2, max_per_day=10)
    import time
    t0 = time.monotonic()
    await lg._throttle("h", cfg)
    await lg._throttle("h", cfg)   # must wait ~0.2s
    assert time.monotonic() - t0 >= 0.18


# ---- grab + worker --------------------------------------------------------------------------

def _enable_libgen(db, **cfg):
    db.add(Integration(kind="libgen", name="OL", base_url="", api_key="", enabled=True,
                       config=cfg or None))
    db.commit()


@pytest.mark.asyncio
async def test_grab_creates_job_with_candidates(db, monkeypatch):
    cw = _cw(db); _enable_libgen(db)

    async def fake_search(db_, cw_, cfg, fetcher):
        return [lg.Hit("libgen", "Pride and Prejudice", "Jane Austen", "epub", 1, 2010, "en",
                       "a"*32, "libgen.la", "p", None)]
    monkeypatch.setattr(lg, "search_book", fake_search)
    job = await lg.grab(db, cw, user_id=1)
    assert job is not None and job.grab_kind == "libgen" and job.status == "queued"
    assert len(job.candidates) == 1 and job.candidates[0]["md5"] == "a"*32


@pytest.mark.asyncio
async def test_grab_returns_none_when_no_hits(db, monkeypatch):
    cw = _cw(db); _enable_libgen(db)
    monkeypatch.setattr(lg, "search_book", lambda *a, **k: _coro([]))
    assert await lg.grab(db, cw, user_id=1) is None


@pytest.mark.asyncio
async def test_grab_dedups_same_user_same_book(db, monkeypatch):
    """F22: a second libgen grab for the same book + user reuses the in-flight job instead of
    creating a duplicate DownloadJob (which would duplicate-download + duplicate-import)."""
    cw = _cw(db); _enable_libgen(db)

    async def fake_search(db_, cw_, cfg, fetcher):
        return [lg.Hit("libgen", "Pride and Prejudice", "Jane Austen", "epub", 1, 2010, "en",
                       "a"*32, "libgen.la", "p", None)]
    monkeypatch.setattr(lg, "search_book", fake_search)
    first = await lg.grab(db, cw, user_id=1)
    second = await lg.grab(db, cw, user_id=1)
    assert first is not None and second is not None and second.id == first.id  # reused, not duplicated
    from sqlalchemy import func, select
    from app.models import DownloadJob
    n = db.scalar(select(func.count(DownloadJob.id)).where(DownloadJob.catalog_work_id == cw.id))
    assert n == 1


def _coro(v):
    async def _c(): return v
    return _c()


@pytest.mark.asyncio
async def test_advance_job_imports_then_stops(db, monkeypatch, tmp_path):
    cw = _cw(db)
    job = DownloadJob(catalog_work_id=cw.id, title=cw.title, status="queued", grab_kind="libgen",
                      attempt=0, candidates=[{"provider": "libgen", "md5": "a"*32, "ext": "epub",
                                              "host": "libgen.la", "title": cw.title, "key": "a"*32}])
    db.add(job); db.commit(); db.refresh(job)
    cfg = _cfg(download_dir=str(tmp_path))
    f = lg.Fetcher(cfg)

    async def fake_dl(fetcher, hit, cfg_, dest):
        open(dest, "wb").write(b"x" * 5000)   # pretend we downloaded a file
        return "ok"
    monkeypatch.setattr(lg, "_resolve_download", fake_dl)
    monkeypatch.setattr(lg, "_import_file", lambda db_, p, c, j, t: _set(j, "imported"))

    await lg._advance_job(db, job, cfg, f, str(tmp_path))
    assert job.status == "imported" and job.attempt == 0   # stopped on first success


@pytest.mark.asyncio
async def test_advance_job_cascades_then_fails(db, monkeypatch, tmp_path):
    from app.ingestion import ledger, source_state
    cw = _cw(db)
    req = ledger._upsert(db, cw)                      # seed the ledger row + libgen source child
    source_state.ensure_rows(db, req, ["libgen"])
    cands = [{"provider": "libgen", "md5": x*32, "ext": "epub", "host": "libgen.la",
              "title": cw.title, "key": x*32} for x in ("a", "b")]
    job = DownloadJob(catalog_work_id=cw.id, title=cw.title, status="queued", grab_kind="libgen",
                      attempt=0, candidates=cands)
    db.add(job); db.commit(); db.refresh(job)
    cfg = _cfg(download_dir=str(tmp_path))
    f = lg.Fetcher(cfg)

    async def fail_dl(fetcher, hit, cfg_, dest):
        return "fail"   # every candidate is a terminally dead/wrong link
    monkeypatch.setattr(lg, "_resolve_download", fail_dl)
    await lg._advance_job(db, job, cfg, f, str(tmp_path))
    assert job.status == "failed" and job.attempt == 2   # tried both, then gave up
    # Wave B additive: some candidates were tried but none verified → libgen source is 'exhausted'.
    row = db.scalar(select(WorkSourceSearch).where(
        WorkSourceSearch.content_request_id == req.id, WorkSourceSearch.source == "libgen"))
    assert row.status == "exhausted"


@pytest.mark.asyncio
async def test_advance_job_transient_block_fails_and_ledgers(db, monkeypatch, tmp_path):
    """A blocked/timed-out endpoint FAILS the job and records the title unavailable in the missing
    ledger (reason="blocked") — it does NOT requeue/retry in place (no 1/6 backoff loop), and never
    advances the candidate or blacklists the link (the link isn't dead, just temporarily blocked)."""
    from app.ingestion import broken
    from app.models import ContentRequest
    cw = _cw(db)
    job = DownloadJob(catalog_work_id=cw.id, title=cw.title, status="queued", grab_kind="libgen",
                      attempt=0, candidates=[{"provider": "libgen", "md5": "a"*32, "ext": "epub",
                                              "host": "libgen.la", "title": cw.title, "key": "a"*32}])
    db.add(job); db.commit(); db.refresh(job)
    cfg = _cfg(download_dir=str(tmp_path))
    f = lg.Fetcher(cfg)

    async def blocked_dl(fetcher, hit, cfg_, dest):
        return "throttled"   # endpoint blocked/overloaded — transient
    monkeypatch.setattr(lg, "_resolve_download", blocked_dl)

    await lg._advance_job(db, job, cfg, f, str(tmp_path))
    assert job.status == "failed" and job.attempt == 0       # not advanced, not retried in place
    assert "a"*32 not in broken.broken_keys(db)              # a transient block never blacklists
    row = db.scalar(select(ContentRequest).where(ContentRequest.norm_key == cw.norm_key))
    assert row is not None and row.status == "unavailable" and row.failure_reason == "blocked"
    assert row.next_check_at is not None                     # ledger owns the throttled re-check


def _set(job, status):
    job.status = status
    return status


@pytest.mark.asyncio
async def test_worker_fails_when_no_download_dir(db, monkeypatch):
    # Configured libgen but no download_dir and no SABnzbd → must NOT import into a temp dir.
    _enable_libgen(db)   # config={} → no download_dir
    cw = _cw(db)
    job = DownloadJob(catalog_work_id=cw.id, title=cw.title, status="queued", grab_kind="libgen",
                      attempt=0, candidates=[{"provider": "libgen", "md5": "a"*32, "ext": "epub",
                                              "host": "libgen.la", "key": "a"*32}])
    db.add(job); db.commit(); db.refresh(job)
    out = await lg.libgen_tick()
    db.refresh(job)
    assert job.status == "failed" and "download directory" in (job.error or "")
    assert out.get("error") == "no download_dir"


def test_integration_out_redacts_annas_key(db):
    from app.routers.integrations import _to_out
    integ = Integration(kind="libgen", name="OL", base_url="", api_key="", enabled=True,
                        config={"providers": ["annas"], "annas_key": "secret-membership-key"})
    db.add(integ); db.commit(); db.refresh(integ)
    out = _to_out(db, integ)
    assert "annas_key" not in out.config            # the secret is never returned
    assert out.config.get("annas_key_set") is True  # but the UI can tell it's set


def test_update_preserves_unsent_annas_key(db):
    """Editing other fields without resending the secret must not wipe annas_key (write-only)."""
    from app.routers.integrations import update_integration
    from app.schemas import IntegrationUpdate
    integ = Integration(kind="libgen", name="OL", base_url="", api_key="", enabled=True,
                        config={"providers": ["annas"], "annas_key": "keep-me"})
    db.add(integ); db.commit(); db.refresh(integ)
    # UI re-saves the redacted config (annas_key absent, annas_key_set flag echoed back) → key kept.
    update_integration(integ.id, IntegrationUpdate(
        config={"providers": ["annas"], "annas_key_set": True}), db)
    db.refresh(integ)
    assert integ.config["annas_key"] == "keep-me"
    assert "annas_key_set" not in integ.config       # the read-only flag isn't persisted


def test_cf_challenge_vs_origin_error():
    import httpx as _httpx
    # An overloaded origin nginx 503 → NOT a challenge (don't waste a browser attempt).
    origin = _httpx.Response(503, headers={"server": "cloudflare"})
    assert lg._is_cf_challenge(origin, b"<html><body>503 Service Temporarily Unavailable nginx</body></html>") is False
    # A real Cloudflare anti-bot challenge → blocked (worth a browser retry).
    chal_hdr = _httpx.Response(403, headers={"cf-mitigated": "challenge"})
    assert lg._is_cf_challenge(chal_hdr, b"") is True
    chal_body = _httpx.Response(503, headers={})
    assert lg._is_cf_challenge(chal_body, b"<title>Just a moment...</title> cf-chl") is True


def test_is_importable_file_rejects_mobi(tmp_path):
    epub = tmp_path / "a.epub"; epub.write_bytes(b"PK\x03\x04rest-of-zip")
    pdf = tmp_path / "a.pdf"; pdf.write_bytes(b"%PDF-1.7 ...")
    mobi = tmp_path / "a.mobi"; mobi.write_bytes(b"Think and Grow Rich\x00" + b"\x00"*40 + b"BOOKMOBI")
    assert lg._is_importable_file(str(epub)) is True
    assert lg._is_importable_file(str(pdf)) is True
    assert lg._is_importable_file(str(mobi)) is False   # PDB/mobi header is ASCII but not a book container


@pytest.mark.asyncio
async def test_search_runs_annas_only(db, monkeypatch):
    """Anna's Archive is the sole provider now; search_book runs it and ranks its hits (no fallback)."""
    cw = _cw(db)
    f = lg.Fetcher(_cfg())
    called = []

    async def annas_hit(fetcher, cfg, cw, titles):
        called.append("annas")
        return [lg.Hit("annas", "Pride and Prejudice", "Jane Austen", "epub", 1, 2010, "en", "a"*32,
                       None, None, None)]
    monkeypatch.setitem(lg._PROVIDERS, "annas", annas_hit)

    out = await lg.search_book(db, cw, _cfg(), f)
    assert [h.md5 for h in out] == ["a"*32]
    assert called == ["annas"]   # only Anna's ran; ALL_PROVIDERS is annas-only


# ---- Anna's Archive fast-download fallback (the only route past the dead libgen CDN) ----------
def _annas_hit(md5="a" * 32):
    return lg.Hit(provider="annas", title="t", author=None, ext="epub", size=1000, year=None,
                  language=None, md5=md5, host=None, page_url=None, direct_url=None,
                  content_type="book")


@pytest.mark.asyncio
async def test_resolve_download_falls_back_to_annas(monkeypatch, tmp_path):
    """When every libgen mirror fails to resolve the md5, _resolve_download falls back to Anna's
    Archive fast-download (a different download host) and succeeds from there."""
    cfg = _cfg(download_dir=str(tmp_path), annas_key="secret", annas_hosts=["annas-archive.gl"])
    f = lg.Fetcher(cfg)

    async def no_mirror(fetcher, host, md5):
        return None                       # every libgen mirror ads/get page fails to resolve
    monkeypatch.setattr(lg, "_libgen_get_url", no_mirror)

    async def fake_get_json(url, *, params=None):
        assert "fast_download.json" in url and params["key"] == "secret"
        return {"download_url": "https://partner.example/file.epub"}
    monkeypatch.setattr(f, "get_json", fake_get_json)

    fetched = {}

    async def fake_fetch(fetcher, url, dest, *, referer=None, render_host=None):
        fetched["url"] = url
        return "ok"
    monkeypatch.setattr(lg, "_fetch_with_fallback", fake_fetch)

    st = await lg._resolve_download(f, _annas_hit(), cfg, str(tmp_path / "out.epub"))
    assert st == "ok" and fetched["url"] == "https://partner.example/file.epub"


@pytest.mark.asyncio
async def test_resolve_download_no_annas_key_skips_fast(monkeypatch, tmp_path):
    """Without a membership key the fast-download API is never called; mirror failure is transient."""
    cfg = _cfg(download_dir=str(tmp_path), annas_key=None)
    f = lg.Fetcher(cfg)

    async def no_mirror(fetcher, host, md5):
        return None
    monkeypatch.setattr(lg, "_libgen_get_url", no_mirror)

    called = {"json": False}

    async def fake_get_json(url, *, params=None):
        called["json"] = True
        return {"download_url": "x"}
    monkeypatch.setattr(f, "get_json", fake_get_json)

    st = await lg._resolve_download(f, _annas_hit(), cfg, str(tmp_path / "out.epub"))
    assert st == "throttled" and called["json"] is False   # mirrors all "throttled", AA not tried


@pytest.mark.asyncio
async def test_annas_fast_url_host_failover_and_error(monkeypatch, tmp_path):
    cfg = _cfg(annas_key="k", annas_hosts=["h1", "h2"])
    f = lg.Fetcher(cfg)

    seq = {"h1": None, "h2": {"download_url": "https://h2/file"}}   # h1 unreachable → h2 answers

    async def fake_get_json(url, *, params=None):
        host = url.split("/")[2]
        return seq[host]
    monkeypatch.setattr(f, "get_json", fake_get_json)
    assert await lg._annas_fast_url(f, cfg, "a" * 32) == "https://h2/file"

    async def bad_key(url, *, params=None):
        return {"download_url": None, "error": "Invalid secret key"}
    monkeypatch.setattr(f, "get_json", bad_key)
    assert await lg._annas_fast_url(f, cfg, "a" * 32) is None
