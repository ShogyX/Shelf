"""Curated matching-quality improvements in the fetch pipeline (fetch-ledger-2026-06).

Each block pins an improvement AND a guard that the change doesn't admit an obvious false positive:
  1. cascade early-abort when the remaining tail is all weak speculative matches (downloads.py)
  2. boxset hard-reject for a single-title request (release_matcher.py)
  3. author-less gate relaxed by a strong ALT-title match, prose-without-alt stays strict (release_matcher.py)
  4. release/hit scored against ALL known title variants, best wins (release_matcher.py + libgen.py)
  5. libgen candidate floor loosened for long titles / author-surname match, short titles stay gated (libgen.py)
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from sqlalchemy import delete, select

import app.ingestion.adapters  # noqa: F401  — register the local_folder adapter
from app.db import SessionLocal, init_db
from app.ingestion import broken
from app.ingestion import downloads as dl
from app.ingestion import libgen as lg
from app.ingestion import release_matcher as rm
from app.integrations.sabnzbd import SABnzbdClient
from app.models import BrokenRelease, CatalogWork, DownloadJob, Integration, UsenetGrab


async def _no_del(self, nzo_id, *, del_files=False):
    return {"status": True}


# ============================================================ 2. boxset hard-reject (single-title)
@dataclass
class FakeRelease:
    title: str
    size: int = 10_000_000
    categories: list = field(default_factory=lambda: [7000, 7020])
    grabs: int = 0
    download_url: str | None = "http://idx/nzb"


def _prefs(**over):
    base = rm.search_prefs(None)
    base.update(over)
    return base


def test_boxset_rejected_for_single_title():
    """A single-title request must REJECT a boxset/omnibus outright (not merely refuse to auto-grab):
    accepting it lets the cascade download a multi-work bundle and fail verification."""
    prefs = _prefs(preferred_formats=["epub"])
    for name in ("Brandon.Sanderson-Mistborn.Trilogy.Omnibus.EPUB",
                 "Brandon.Sanderson-Mistborn.Books.1-3.EPUB",
                 "Brandon.Sanderson-Mistborn.Collection.EPUB"):
        sr = rm.score_release("Mistborn", "Brandon Sanderson", "en", FakeRelease(name), prefs)
        assert not sr.accepted, name
        assert "boxset" in sr.reason


def test_boxset_accepted_when_operator_opts_in():
    """The reject is gated by allow_boxsets — an operator who wants bundles still gets them as
    (speculative, never-auto) candidates."""
    prefs = _prefs(preferred_formats=["epub"], allow_boxsets=True)
    sr = rm.score_release("Mistborn", "Brandon Sanderson", "en",
                          FakeRelease("Brandon.Sanderson-Mistborn.Trilogy.Omnibus.EPUB"), prefs)
    assert sr.accepted and not sr.auto_ok      # bundle accepted, but still never auto-grabbed


def test_single_title_release_still_accepted_guard():
    """Guard: the boxset reject must not touch an ordinary single-title release."""
    prefs = _prefs(preferred_formats=["epub"])
    sr = rm.score_release("Mistborn", "Brandon Sanderson", "en",
                          FakeRelease("Brandon.Sanderson-Mistborn.Retail.EPUB"), prefs)
    assert sr.accepted and sr.auto_ok


# ===================================================== 3. author-less gate relaxed by alt-title
def test_authorless_strong_alt_title_relaxes_gate():
    """A work with no author but a strong NON-CANONICAL alt-title match clears the RELAXED floor.
    The release matches the English alt title at conf 0.8 — below the old strict NO_AUTHOR_MIN_CONF
    (0.9) but at the relaxed ALT_TITLE_MIN_CONF (0.8) — so it's accepted only because of this change.
    (The canonical CJK-transliteration title scores 0 against the release.)"""
    prefs = _prefs(preferred_formats=["epub"])
    canon = "Sekai no Owari to Hadoboirudo Wandarando"
    alt = "Hard Boiled Wonderland End World"
    rel = FakeRelease("Hard.Boiled.Wonderland.End.EPUB")
    sr = rm.score_release(canon, None, "en", rel, prefs, titles=[canon, alt])
    assert sr.accepted
    # The accepted confidence sits in the relaxed band — strict 0.9 would have rejected it.
    assert rm.ALT_TITLE_MIN_CONF <= sr.confidence < rm.NO_AUTHOR_MIN_CONF
    # Without the alt title (canonical only), the same release is rejected (0 recall) — the alt is
    # what carries the match.
    sr0 = rm.score_release(canon, None, "en", rel, prefs, titles=[canon])
    assert not sr0.accepted


def test_authorless_prose_without_alt_stays_strict():
    """Guard: author-less PROSE with only the canonical title stays strict at NO_AUTHOR_MIN_CONF —
    a partial title match ('A Tale of Two Cities' → release 'Two Cities', recall 0.67) is rejected."""
    prefs = _prefs(preferred_formats=["epub"])
    rel = FakeRelease("Two.Cities.EPUB")
    sr = rm.score_release("A Tale of Two Cities", None, "en", rel, prefs)
    assert not sr.accepted and sr.confidence < rm.NO_AUTHOR_MIN_CONF
    # And a WEAK alt match (recall 0.67) must NOT relax the gate either — the alt must be STRONG.
    sr2 = rm.score_release("A Tale of Two Cities", None, "en", rel, prefs,
                           titles=["A Tale of Two Cities", "Two Cities Chronicle"])
    assert not sr2.accepted
    # …but a STRONG alt match (the alt title exactly = the release) DOES relax it (the improvement).
    sr3 = rm.score_release("A Tale of Two Cities", None, "en", rel, prefs,
                           titles=["A Tale of Two Cities", "Two Cities"])
    assert sr3.accepted


def test_authorless_alt_relax_floor_is_above_match_floor():
    """Guard: the relaxed floor stays above the bare MATCH_FLOOR, so a low-recall alt can't sneak
    through — only a genuinely strong alt-title match (>= ALT_TITLE_MIN_CONF) relaxes the gate."""
    assert rm.ALT_TITLE_MIN_CONF > rm.MATCH_FLOOR
    assert rm.ALT_TITLE_MIN_CONF < rm.NO_AUTHOR_MIN_CONF


# ================================================ 4. scoring across ALL title variants (best wins)
def test_release_scored_against_all_title_variants():
    """A release named with the romaji/native title still matches a work catalogued under its
    English title, because scoring takes the MAX over every known title."""
    prefs = _prefs(preferred_formats=["cbz", "cbr"], comic_formats=["cbz", "cbr"])
    # Comic prefs (author-less is structurally fine for comics).
    cprefs = rm.search_prefs(None, media_kind="comic")
    rel = FakeRelease("Shingeki.no.Kyojin.v01.CBZ", categories=[7030])
    only_canonical = rm.score_release("Attack on Titan", None, "ja", rel, cprefs,
                                      titles=["Attack on Titan"])
    with_alt = rm.score_release("Attack on Titan", None, "ja", rel, cprefs,
                                titles=["Attack on Titan", "Shingeki no Kyojin"])
    assert with_alt.confidence > only_canonical.confidence
    assert with_alt.confidence >= 0.9


def test_libgen_score_hit_max_over_titles():
    """libgen._score_hit also takes the best score across all known titles (CJK/translit included)."""
    from app.ingestion.matchmeta import WorkMeta
    meta = WorkMeta(titles=["Attack on Titan", "Shingeki no Kyojin"], author=None, language="ja",
                    bucket="comic", media_kind="comic")
    h = lg.Hit("libgen", "Shingeki no Kyojin Vol 1", None, "cbz", 1, None, "ja", "a" * 32,
               "libgen.la", None, None, content_type="Comic")
    assert lg._score_hit(meta, h) >= 0.85
    # Guard: an unrelated title scores low even though it's one extra variant in the list.
    meta2 = WorkMeta(titles=["Attack on Titan", "Shingeki no Kyojin"], author=None, language="ja",
                     bucket="comic", media_kind="comic")
    h2 = lg.Hit("libgen", "Completely Different Manga Vol 1", None, "cbz", 1, None, "ja", "b" * 32,
                "libgen.la", None, None, content_type="Comic")
    assert lg._score_hit(meta2, h2) < 0.5


# ====================================== 5. libgen candidates_for long-title floor loosen (gated)
def _cfg(**over):
    base = dict(providers=["libgen"], libgen_hosts=["libgen.la"], min_interval_s=0.0,
                max_per_day=1000, max_concurrent=2, formats=["epub", "pdf"], download_dir=None,
                zlib_user=None, zlib_pass=None)
    base.update(over)
    return lg.Config(**base)


def _meta(title, author=None, titles=None, bucket="prose"):
    from app.ingestion.matchmeta import WorkMeta
    return WorkMeta(titles=titles or [title], author=author, language="en", bucket=bucket,
                    media_kind="text")


def test_long_title_uses_loosened_floor():
    """A LONG title (>= 6 significant words) gets the loosened candidate floor — so a correct hit
    that drops articles/subtitle words and dips just below the strict 0.5 is still kept."""
    meta = _meta("The Lord of the Rings: The Fellowship of the Ring", author="J.R.R. Tolkien")
    h = lg.Hit("libgen", "Fellowship of the Ring", "Tolkien, J.R.R.", "epub", 1_000_000, 1954,
               "en", "a" * 32, "libgen.la", None, None, content_type="Book")
    assert lg._candidate_floor(meta, h) == lg.CANDIDATE_FLOOR_LONG


def test_candidate_in_loosened_band_admitted_only_for_long_title(monkeypatch):
    """A hit scoring in the loosened band [0.45, 0.5) is ADMITTED for a long title but DROPPED for a
    short one — pinned with a fixed _score_hit so the admission decision is exactly the floor."""
    band_score = (lg.CANDIDATE_FLOOR_LONG + lg.CANDIDATE_FLOOR) / 2   # 0.475, strictly between
    monkeypatch.setattr(lg, "_score_hit", lambda meta, h: band_score)
    long_meta = _meta("The Curious Incident of the Dog in the Night Time", author="Mark Haddon")
    short_meta = _meta("Dune", author="Frank Herbert")
    h = lg.Hit("libgen", "whatever", "Nobody", "epub", 1_000_000, 2003, "en", "a" * 32,
               "libgen.la", None, None, content_type="Book")
    assert [c.md5 for c in lg.candidates_for(long_meta, [h], _cfg())] == ["a" * 32]  # long → kept
    assert lg.candidates_for(short_meta, [h], _cfg()) == []                          # short → dropped


def test_short_title_still_strictly_gated_guard():
    """Guard: for a SHORT title the floor stays strict — a sub-0.5 score really is a different book
    and must be dropped (no author surname match, title not long)."""
    meta = _meta("Dune", author="Frank Herbert")
    # A different short book whose score against 'Dune' is below 0.5 and whose author differs.
    h = lg.Hit("libgen", "Dunes and Deserts", "Some Geographer", "epub", 1_000_000, 2001, "en",
               "c" * 32, "libgen.la", None, None, content_type="Book")
    assert lg._candidate_floor(meta, h) == lg.CANDIDATE_FLOOR
    assert lg.candidates_for(meta, [h], _cfg()) == []


def test_author_surname_match_loosens_floor():
    """The floor also loosens when the author SURNAME matches between work and hit (corroborating
    evidence that a sub-0.5 title score is the right book in a differently-styled edition)."""
    meta = _meta("Some Moderately Titled Book", author="Ursula K. Le Guin")
    h_match = lg.Hit("libgen", "Moderately Titled", "Le Guin, Ursula", "epub", 1, None, "en",
                     "a" * 32, "libgen.la", None, None)
    h_nomatch = lg.Hit("libgen", "Moderately Titled", "Someone Else", "epub", 1, None, "en",
                       "b" * 32, "libgen.la", None, None)
    assert lg._candidate_floor(meta, h_match) == lg.CANDIDATE_FLOOR_LONG
    assert lg._candidate_floor(meta, h_nomatch) == lg.CANDIDATE_FLOOR


# ================================================= 1. cascade early-abort on a doomed weak tail
def _setup_dl(db):
    db.execute(delete(DownloadJob)); db.execute(delete(CatalogWork)); db.execute(delete(Integration))
    db.execute(delete(BrokenRelease)); db.execute(delete(UsenetGrab))
    db.commit()
    db.add(Integration(kind="sabnzbd", name="SAB", base_url="http://sab", api_key="k",
                       enabled=True, config={"category": "shelf", "library_path": "/tmp/lib"}))
    cw = CatalogWork(provider="openlibrary", provider_ref="/works/X", domain="openlibrary.org",
                     work_url="x", title="Project Hail Mary", author="Andy Weir",
                     media_kind="text", norm_key="project hail mary")
    db.add(cw); db.commit(); db.refresh(cw)
    return cw


def test_remaining_all_doomed_helper():
    """The doom check: all remaining usable candidates below the floor → True; a plausible one
    (>= floor) or a confidence-less one → False; no usable candidates → False (normal exhaustion)."""
    j = DownloadJob(attempt=0, candidates=[
        {"key": "k0", "download_url": "u0", "confidence": 0.9},   # current (index 0)
        {"key": "k1", "download_url": "u1", "confidence": 0.5},   # weak
        {"key": "k2", "download_url": "u2", "confidence": 0.4},   # weak
    ])
    assert dl._remaining_all_doomed(j, set(), start=1) is True
    j2 = DownloadJob(attempt=0, candidates=[
        {"key": "k0", "download_url": "u0", "confidence": 0.9},
        {"key": "k1", "download_url": "u1", "confidence": 0.5},
        {"key": "k2", "download_url": "u2", "confidence": 0.7},   # one still plausible (>= 0.65)
    ])
    assert dl._remaining_all_doomed(j2, set(), start=1) is False
    j3 = DownloadJob(attempt=0, candidates=[
        {"key": "k0", "download_url": "u0", "confidence": 0.9},
        {"key": "k1", "download_url": "u1"},                      # no confidence → treated plausible
    ])
    assert dl._remaining_all_doomed(j3, set(), start=1) is False
    # No remaining usable candidates → not "doomed" (that's ordinary exhaustion).
    assert dl._remaining_all_doomed(j, set(), start=3) is False


@pytest.mark.asyncio
async def test_grab_next_aborts_doomed_weak_tail(monkeypatch):
    """When the current candidate fails and every remaining one is a weak speculative match, the
    cascade is abandoned (job failed) instead of grinding through them."""
    init_db(); db = SessionLocal(); cw = _setup_dl(db)
    sab = db.scalar(select(Integration).where(Integration.kind == "sabnzbd"))
    job = DownloadJob(catalog_work_id=cw.id, title="Project Hail Mary", nzo_id="nzoA",
                      status="downloading", attempt=0, grab_kind="manual", candidates=[
                          {"key": "g0", "download_url": "u0", "confidence": 0.92},   # current, failing
                          {"key": "g1", "download_url": "u1", "confidence": 0.5},    # weak
                          {"key": "g2", "download_url": "u2", "confidence": 0.45},   # weak
                      ])
    db.add(job); db.commit(); db.refresh(job)

    enqueued = []

    async def fake_add(self, url, *, category=None, nzbname=None, priority=None):
        enqueued.append(url)
        return {"nzo_ids": ["nzoB"]}
    monkeypatch.setattr(SABnzbdClient, "add_url", fake_add)
    monkeypatch.setattr(SABnzbdClient, "delete_history", _no_del)

    gn = await dl._grab_next(db, job, sab, reason="verify failed")
    db.refresh(job)
    assert gn == "failed"
    assert job.status == "failed"
    assert enqueued == []                                  # never tried a weak candidate
    assert broken.is_broken(db, {"key": "g0"})             # the failed current one is marked broken
    db.close()


@pytest.mark.asyncio
async def test_grab_next_advances_when_plausible_candidate_remains(monkeypatch):
    """Guard: a remaining candidate at/above the abort floor is ALWAYS tried — the early-abort must
    not fire while a plausibly-correct candidate is left."""
    init_db(); db = SessionLocal(); cw = _setup_dl(db)
    sab = db.scalar(select(Integration).where(Integration.kind == "sabnzbd"))
    job = DownloadJob(catalog_work_id=cw.id, title="Project Hail Mary", nzo_id="nzoA",
                      status="downloading", attempt=0, grab_kind="manual", candidates=[
                          {"key": "g0", "download_url": "u0", "confidence": 0.92},   # current, failing
                          {"key": "g1", "download_url": "u1", "confidence": 0.5},    # weak
                          {"key": "g2", "download_url": "u2", "confidence": 0.7},    # still plausible
                      ])
    db.add(job); db.commit(); db.refresh(job)

    async def fake_add(self, url, *, category=None, nzbname=None, priority=None):
        return {"nzo_ids": ["nzoB"]}
    monkeypatch.setattr(SABnzbdClient, "add_url", fake_add)
    monkeypatch.setattr(SABnzbdClient, "delete_history", _no_del)

    gn = await dl._grab_next(db, job, sab, reason="verify failed")
    db.refresh(job)
    assert gn == "queued"
    assert job.nzo_id == "nzoB" and job.attempt == 1      # advanced to the next candidate
    db.close()


@pytest.mark.asyncio
async def test_grab_next_fuzz_does_not_early_abort(monkeypatch):
    """Guard: fuzz deliberately tries the low-confidence long tail; the early-abort must NOT fire for
    a fuzz job."""
    init_db(); db = SessionLocal(); cw = _setup_dl(db)
    sab = db.scalar(select(Integration).where(Integration.kind == "sabnzbd"))
    job = DownloadJob(catalog_work_id=cw.id, title="Project Hail Mary", nzo_id="nzoA",
                      status="downloading", attempt=0, grab_kind="fuzz", candidates=[
                          {"key": "g0", "download_url": "u0", "confidence": 0.4},
                          {"key": "g1", "download_url": "u1", "confidence": 0.35},
                      ])
    db.add(job); db.commit(); db.refresh(job)

    async def fake_add(self, url, *, category=None, nzbname=None, priority=None):
        return {"nzo_ids": ["nzoB"]}
    monkeypatch.setattr(SABnzbdClient, "add_url", fake_add)
    monkeypatch.setattr(SABnzbdClient, "delete_history", _no_del)

    gn = await dl._grab_next(db, job, sab, reason="verify failed")
    db.refresh(job)
    assert gn == "queued" and job.attempt == 1            # fuzz still advances through the weak tail
    db.close()
