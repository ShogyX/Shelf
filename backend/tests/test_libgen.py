"""Open-library fallback pipeline: rate limiter, result parsing, candidate ranking, and the
download→verify→import worker (network fully mocked)."""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import delete, select

from app.db import SessionLocal, init_db
from app.ingestion import libgen as lg
from app.models import CatalogWork, DownloadJob, Integration


@pytest.fixture
def db():
    init_db()
    s = SessionLocal()
    for m in (DownloadJob, CatalogWork, Integration):
        s.execute(delete(m))
    s.commit()
    lg._HOSTS.clear()
    yield s
    s.close()


def _cfg(**over):
    base = dict(providers=["libgen"], libgen_hosts=["libgen.la", "libgen.gl"], min_interval_s=0.0,
               max_per_day=1000, max_concurrent=2, formats=["epub", "pdf"], download_dir=None,
               zlib_user=None, zlib_pass=None)
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
    cw = _cw(db)
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


@pytest.mark.asyncio
async def test_advance_job_requeues_on_transient_block_then_gives_up(db, monkeypatch, tmp_path):
    """A blocked/timed-out endpoint keeps the job QUEUED (with backoff) and retried — never advancing
    the candidate or blacklisting it — until it has failed MAX_TRANSIENT_RETRIES times, then fails."""
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

    # Each pass re-queues with backoff (candidate NOT advanced, NOT blacklisted) until the cap.
    for n in range(1, lg.MAX_TRANSIENT_RETRIES + 1):
        await lg._advance_job(db, job, cfg, f, str(tmp_path))
        assert job.status == "queued" and job.attempt == 0 and job.retries == n
        assert job.not_before is not None
        from app.ingestion import broken
        assert "a"*32 not in broken.broken_keys(db)   # a transient block never blacklists the link
    # One more pass exceeds the cap → terminal failure.
    await lg._advance_job(db, job, cfg, f, str(tmp_path))
    assert job.status == "failed" and "giving up" in (job.error or "")


@pytest.mark.asyncio
async def test_stock_path_never_left_queued_on_transient(db, monkeypatch, tmp_path):
    """The stock path drives _advance_job synchronously and must never leave a job queued (the worker
    would then import it into the library, not the stock dir) — a transient there just ends as failed
    and the stock layer recycles the item."""
    cw = _cw(db)
    job = DownloadJob(catalog_work_id=cw.id, title=cw.title, status="queued", grab_kind="libgen",
                      attempt=0, candidates=[{"provider": "libgen", "md5": "a"*32, "ext": "epub",
                                              "host": "libgen.la", "title": cw.title, "key": "a"*32}])
    db.add(job); db.commit(); db.refresh(job)
    cfg = _cfg(download_dir=str(tmp_path))
    f = lg.Fetcher(cfg)

    async def blocked_dl(fetcher, hit, cfg_, dest):
        return "throttled"
    monkeypatch.setattr(lg, "_resolve_download", blocked_dl)
    await lg._advance_job(db, job, cfg, f, str(tmp_path), requeue_on_transient=False)
    assert job.status == "failed" and job.retries == 0   # not requeued


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


def test_integration_out_redacts_zlib_pass(db):
    from app.routers.integrations import _to_out
    integ = Integration(kind="libgen", name="OL", base_url="", api_key="", enabled=True,
                        config={"providers": ["zlibrary"], "zlib_user": "me@x.com", "zlib_pass": "secret"})
    db.add(integ); db.commit(); db.refresh(integ)
    out = _to_out(db, integ)
    assert "zlib_pass" not in out.config            # the secret is never returned
    assert out.config.get("zlib_pass_set") is True  # but the UI can tell it's set
    assert out.config.get("zlib_user") == "me@x.com"


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
async def test_search_falls_back_to_browser_providers_only_when_empty(db, monkeypatch):
    """Fast providers (libgen/annas) run first; the slow browser providers (zlibrary/oceanofpdf) are
    tried ONLY when the fast ones found nothing importable — so the common case stays fast."""
    cw = _cw(db)
    f = lg.Fetcher(_cfg(providers=["libgen", "annas", "zlibrary", "oceanofpdf"]))
    called = []

    async def fast_hit(fetcher, cfg, cw, titles):
        called.append("libgen")
        return [lg.Hit("libgen", "Pride and Prejudice", "Jane Austen", "epub", 1, 2010, "en", "a"*32,
                       "libgen.la", None, None)]
    async def empty(fetcher, cfg, cw, titles):
        called.append("other"); return []
    monkeypatch.setitem(lg._PROVIDERS, "libgen", fast_hit)
    monkeypatch.setitem(lg._PROVIDERS, "annas", empty)
    monkeypatch.setitem(lg._PROVIDERS, "zlibrary", empty)
    monkeypatch.setitem(lg._PROVIDERS, "oceanofpdf", empty)

    out = await lg.search_book(db, cw, _cfg(providers=["libgen", "annas", "zlibrary", "oceanofpdf"]), f)
    assert [h.md5 for h in out] == ["a"*32]
    assert called == ["libgen", "other"]   # only the fast providers ran; browser fallback NOT used

    # Now the fast providers find nothing → the browser fallback IS used (all four providers run).
    called.clear()
    monkeypatch.setitem(lg._PROVIDERS, "libgen", empty)
    out2 = await lg.search_book(db, cw, _cfg(providers=["libgen", "annas", "zlibrary", "oceanofpdf"]), f)
    assert len(called) == 4 and not out2   # fast (2) + browser fallback (2)
