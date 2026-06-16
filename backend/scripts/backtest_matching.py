"""Read-only A/B backtest for the 2026-06-16 matcher recall changes.

For a sample of currently-STUCK titles (content_requests.status='unavailable'), it runs a fresh
Prowlarr search and scores every returned release with BOTH the OLD and the NEW title/author
confidence formula on the SAME release set — so availability noise cancels out and we measure only
the scoring change. Reports how many titles gain a "tried" candidate (conf >= the cascade floor) and
how many individual releases move out of the old [0.60,0.65) dead-band / author-miss rejection.

It writes NOTHING and grabs NOTHING — it only searches (which the operator authorised). Run BEFORE
deploying the changes to size the recovery; a non-trivial title-level recovery with no obvious false
positives is the green light.

  Usage:  .venv/bin/python scripts/backtest_matching.py [N]      # N = sample size, default 30

Limitation: this measures the PRE-download matcher only. The verify-level wins (ISBN / alt-title /
fuzzy author against embedded file metadata) can't be replayed here because the rejected downloads
were not retained; 'all_broken' titles also can't surface (their releases are in broken_releases).
"""
from __future__ import annotations

import asyncio
import sys

from sqlalchemy import text

from app.db import SessionLocal
from app.ingestion import matchmeta
from app.ingestion.extract import norm_title
from app.ingestion.release_matcher import (
    _NOISE_TOKENS,
    _STOPWORDS,
    _author_tokens,
    build_query,
    get_prowlarr,
    parse_release,
    score_release,
    search_prefs,
    title_author_confidence,  # NEW formula (patched)
)

MATCH_FLOOR = 0.6
OLD_CASCADE_FLOOR = 0.65   # the value before this change


def old_title_author_confidence(book_title: str, book_author: str | None, info) -> float:
    """Faithful copy of title_author_confidence BEFORE the change: exact author tokens only,
    ×0.6 author-absent penalty, no fuzzy author hit."""
    title_toks = set(norm_title(book_title).split())
    sig = title_toks - _NOISE_TOKENS or title_toks - _STOPWORDS or title_toks
    rel = info.content_tokens
    if not sig or not rel:
        return 0.0
    title_toks = sig
    recall = len(title_toks & rel) / len(title_toks)
    if recall == 0.0:
        return 0.0
    author_toks = _author_tokens(book_author)
    author_hit = bool(author_toks & rel) if author_toks else None
    score = recall
    if len(title_toks) < 2:
        if author_hit is not True:
            return 0.0
        score = 1.0
    elif author_hit is False:
        score *= 0.6
    return min(score, 1.0)


async def main(sample: int) -> None:
    db = SessionLocal()
    integ = get_prowlarr(db)
    if integ is None:
        print("No Prowlarr integration configured — cannot backtest."); return
    from app.integrations.prowlarr import ProwlarrClient
    client = ProwlarrClient(integ.base_url, integ.api_key)

    rows = db.execute(text("""
        SELECT cw.id, cw.title, cw.author, cw.language, cw.media_kind
        FROM content_requests cr JOIN catalog_works cw ON cw.id = cr.catalog_work_id
        WHERE cr.status='unavailable' AND cr.failure_reason IN ('no_match','all_broken')
              AND cw.title IS NOT NULL AND cw.author IS NOT NULL
        ORDER BY cr.last_attempt_at DESC LIMIT :n
    """), {"n": sample}).fetchall()
    print(f"Sampled {len(rows)} stuck titles.\n")

    tot_titles = 0
    titles_with_releases = 0
    tot_releases = 0
    titles_new_tried = 0
    titles_old_tried = 0
    titles_recovered = 0          # new gets a tried candidate, old did not
    rel_recovered = 0             # releases moving old<floor-or-deadband → new>=cascade floor
    examples: list[str] = []

    for cw_id, title, author, lang_, mk in rows:
        from app.models import CatalogWork
        cw = db.get(CatalogWork, cw_id)
        prefs = search_prefs(integ, media_kind=(mk or "text"))
        meta = await matchmeta.get_work_meta(db, cw, allow_fetch=False)
        try:
            releases = await client.search(
                build_query(title, author), categories=prefs["categories"],
                indexer_ids=prefs["indexer_ids"], protocols=prefs["protocols"], limit=50)
        except Exception as exc:  # noqa: BLE001
            print(f"  search failed for {title!r}: {exc}"); continue
        tot_titles += 1
        tot_releases += len(releases)
        titles_with_releases += bool(releases)
        new_best = old_best = 0.0
        for r in releases:
            info = parse_release(str(getattr(r, "title", "") or ""), getattr(r, "categories", None))
            new_c = max((title_author_confidence(t, author, info) for t in meta.titles), default=0.0)
            old_c = max((old_title_author_confidence(t, author, info) for t in meta.titles), default=0.0)
            new_best, old_best = max(new_best, new_c), max(old_best, old_c)
            # A release the old pipeline would never have downloaded (below the 0.65 cascade floor,
            # i.e. rejected or dead-banded) but the new one tries (>= the new 0.6 cascade floor).
            if new_c >= MATCH_FLOOR and old_c < OLD_CASCADE_FLOOR:
                rel_recovered += 1
        new_tried = new_best >= MATCH_FLOOR
        old_tried = old_best >= OLD_CASCADE_FLOOR
        titles_new_tried += new_tried
        titles_old_tried += old_tried
        if new_tried and not old_tried:
            titles_recovered += 1
            if len(examples) < 12:
                examples.append(f"    {title!r} by {author!r}: new={new_best:.2f} old={old_best:.2f}")

    print(f"Titles searched (had a query run):        {tot_titles}")
    print(f"  titles for which Prowlarr returned ANY:  {titles_with_releases}"
          f"  ({tot_releases} releases total)")
    print(f"  with a TRIED candidate — old formula:   {titles_old_tried}")
    print(f"  with a TRIED candidate — new formula:   {titles_new_tried}")
    print(f"  RECOVERED (new tries, old wouldn't):    {titles_recovered}"
          f"  ({(100*titles_recovered/tot_titles if tot_titles else 0):.0f}% of searched)")
    print(f"  individual releases recovered:          {rel_recovered}")
    if examples:
        print("\n  examples of recovered titles (new vs old best confidence):")
        print("\n".join(examples))
    db.close()


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    asyncio.run(main(n))
