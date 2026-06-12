"""Shared anti-bot challenge detection (F0.4): full-body scans, 200-status challenges,
header semantics, and the store-time backstop that keeps interstitials out of chapters."""
from __future__ import annotations

import pytest
from sqlalchemy import delete, select

from app.db import SessionLocal, init_db
from app.ingestion.challenge import is_challenge, looks_like_challenge_page, via_cloudflare

# A verbose managed-challenge page: padding pushes every marker PAST the historical 4 KB scan
# window, and the page is served with HTTP 200 — both must now be caught.
_PADDING = ("<div>" + ("lorem ipsum dolor sit amet " * 40) + "</div>\n") * 8
_VERBOSE_CHALLENGE = (
    "<html><head><meta charset='utf-8'>"
    + _PADDING
    + "<title>Just a moment...</title></head><body>"
    + "<script>window._cf_chl_opt={cvId: '3'};</script>"
    + "<form id=\"challenge-form\" action=\"/x\"></form>"
    + "Verifying you are human. This may take a few seconds."
    + "</body></html>"
)
assert min(_VERBOSE_CHALLENGE.find("_cf_chl"), _VERBOSE_CHALLENGE.find("Just a moment")) > 4096


def test_full_body_scan_catches_markers_past_4k():
    assert is_challenge(503, {}, _VERBOSE_CHALLENGE) is True
    assert is_challenge(200, {}, _VERBOSE_CHALLENGE) is True   # 200-status challenge


def test_any_cf_mitigated_value_is_a_challenge():
    for v in ("challenge", "managed_challenge", "block"):
        assert is_challenge(200, {"cf-mitigated": v}, "<html>ok</html>") is True
    assert is_challenge(200, {"cf-mitigated": ""}, "<html>ok</html>") is False


def test_prose_quoting_challenge_phrases_is_not_flagged():
    """Regression guardrail: fiction containing 'just a moment'/'captcha' must never be flagged
    on a clean 200 — text markers only convict on suspicious statuses or proven-CF responses."""
    prose = ("<html><body><p>“Just a moment...”, she said, checking your browser history "
             "while the captcha of daily life ticked by. Access denied, he thought.</p>"
             + "<p>real chapter text </p>" * 200 + "</body></html>")
    assert is_challenge(200, {}, prose) is False
    # …and even at 403 the TEXT marker needs to match an interstitial phrase pattern; plain
    # narrative quoting still trips only if it contains a real marker phrase. "just a moment..."
    # IS a marker phrase, so a 403 with it is treated as a block (status already suspicious).
    assert is_challenge(403, {}, prose) is True


def test_via_cloudflare_header_gate():
    assert via_cloudflare({"cf-ray": "8abc-LHR"}) is True
    assert via_cloudflare({"server": "cloudflare"}) is True
    assert via_cloudflare({"server": "nginx"}) is False
    # 200 + CF transit + interstitial text → challenge
    assert is_challenge(200, {"cf-ray": "x"}, "<title>just a moment...</title>") is True


def test_store_backstop_rejects_challenge_keeps_prose():
    # Challenge page: structural marker + short + imageless → rejected.
    short_challenge = ("<html><title>Just a moment...</title>"
                       "<script>window._cf_chl_opt={};</script>"
                       "<body>Checking your browser before accessing.</body></html>")
    assert looks_like_challenge_page(short_challenge) is True
    # Real prose mentioning the phrases (long, no structural artifacts) → kept.
    prose = "<p>" + "just a moment of your time, dear reader. " * 100 + "</p>"
    assert looks_like_challenge_page(prose) is False
    # Comic page (has <img>) → kept even with phrase-y alt text.
    comic = '<div><img src="/media/x.jpg" alt="just a moment"/></div>'
    assert looks_like_challenge_page(comic) is False


def test_store_chapter_content_refuses_challenge_page():
    """The poisoning path: a verbose interstitial that passes the old >50-words dead-end check
    must NOT be persisted — it raises RateLimited and the chapter stays pending."""
    from app.ingestion.base import RawChapter
    from app.ingestion.engine import store_chapter_content
    from app.ingestion.fetcher import RateLimited
    from app.models import Chapter, ChapterContent, Source, Work

    init_db()
    db = SessionLocal()
    for m in (ChapterContent, Chapter, Work, Source):
        db.execute(delete(m))
    db.commit()
    src = Source(key="generic_feed", display_name="gf", adapter_key="generic_feed",
                 tos_permitted=True)
    db.add(src); db.commit()
    w = Work(source_id=src.id, source_work_ref="https://s/x", title="W")
    db.add(w); db.commit()
    ch = Chapter(work_id=w.id, index=1, source_chapter_ref="c1", fetch_status="pending")
    db.add(ch); db.commit(); db.refresh(ch)

    challenge_body = ("<html><title>Just a moment...</title>"
                      "<script>window._cf_chl_opt={cvId:'3'};</script><body>"
                      + "Verifying you are human. This may take a few seconds. " * 20
                      + "</body></html>")   # >50 words → would pass the old dead-end guard
    with pytest.raises(RateLimited):
        store_chapter_content(db, ch, RawChapter(title="t", body=challenge_body, fmt="html"))
    db.expire_all()
    assert db.get(Chapter, ch.id).fetch_status == "pending"      # NOT marked fetched
    assert db.scalar(select(ChapterContent.id)) is None          # nothing persisted

    # …and a real chapter still stores fine.
    real = "<p>" + "actual story content here. " * 60 + "</p>"
    out = store_chapter_content(db, ch, RawChapter(title="t", body=real, fmt="html"))
    assert out == "stored"
    db.close()


def test_rendered_page_carries_headers_and_original_status():
    from app.ingestion.browser import RenderedPage
    p = RenderedPage(status=200, text="<html/>", url="u",
                     headers={"cf-mitigated": "challenge"}, original_status=403)
    assert p.headers["cf-mitigated"] == "challenge" and p.original_status == 403
    legacy = RenderedPage(status=200, text="", url="u")        # defaults stay safe
    assert legacy.headers == {} and legacy.original_status == 200


def test_fetcher_looks_blocked_full_body_and_200_cf():
    from app.ingestion.fetcher import _looks_blocked
    # markers past 4 KB on a 503 — the old [:4096] slice missed this
    assert _looks_blocked(503, {}, lambda: _VERBOSE_CHALLENGE) is True
    # 200 over Cloudflare with interstitial text → blocked (previously never scanned)
    assert _looks_blocked(200, {"server": "cloudflare"},
                          lambda: "<title>Just a moment...</title>") is True
    # clean 200 from a CF-less origin → body never even consulted
    def boom():
        raise AssertionError("body must not be read for a clean non-CF 200")
    assert _looks_blocked(200, {"server": "nginx"}, boom) is False
