"""Read-only torrent SEARCH + MATCH probe — measures matching/search quality WITHOUT downloading.

For N catalog titles it runs the real Prowlarr torrent search (protocols=torrent) + the release_matcher
and reports, per title and aggregate: raw torrent results, the top ACCEPTED candidate (the one the
route would grab) with confidence/score/seeders/reason, and flags (0-seeder top pick, boxset/pack top
pick, author-mismatch). Use the output to tune torrent search params + matcher gates before the live
download E2E. Searches only — grabs/writes NOTHING.

  Usage:  .venv/bin/python scripts/torrent_search_probe.py [N]      # default N=40
"""
from __future__ import annotations

import asyncio
import sys

from sqlalchemy import func, select

from app.db import SessionLocal
from app.ingestion import release_matcher as rm
from app.models import CatalogWork


def _sample(db, n, popular=True):
    """By default sample the MOST POPULAR un-hooked titles — those are the ones that actually exist on
    torrent trackers, so they stress the matcher (many results → wrong-match risk). Random sampling of
    this catalog mostly hits obscure public-domain works absent from trackers (NO-RESULT noise)."""
    order = CatalogWork.popularity.desc() if popular else func.random()
    return list(db.scalars(
        select(CatalogWork.id).where(CatalogWork.hooked_work_id.is_(None),
                                     CatalogWork.author.is_not(None))
        .order_by(order).limit(n)).all())


async def _probe(db, cw_id):
    cw = db.get(CatalogWork, cw_id)
    ranked = await rm.find_releases(db, cw, protocols=("torrent",))
    raw = len(ranked)
    accepted = [s for s in ranked if s.accepted and getattr(s.release, "download_url", None)]
    top = accepted[0] if accepted else None
    if top is None:
        return {"title": cw.title, "raw": raw, "verdict": "NO-RESULT", "top": None}
    r = top.release
    flags = []
    seeders = getattr(r, "seeders", None)
    if seeders is not None and seeders == 0:
        flags.append("0-seeders")
    tl = (getattr(r, "title", "") or "").lower()
    if any(w in tl for w in ("boxset", "box set", "collection", "complete series", "books 1", "1-3", "1-5", "vol 1-")):
        flags.append("pack?")
    return {"title": cw.title, "author": cw.author, "raw": raw,
            "verdict": "AUTO" if top.auto_ok else "ACCEPTED",
            "top": {"rel": getattr(r, "title", ""), "conf": round(top.confidence, 2),
                    "score": round(top.score, 2), "seeders": seeders, "reason": top.reason},
            "flags": flags}


async def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 40
    db = SessionLocal()
    ids = _sample(db, n)
    print(f"torrent search+match probe: {len(ids)} titles\n")
    agg = {"AUTO": 0, "ACCEPTED": 0, "NO-RESULT": 0}
    flagged = []
    raw_total = 0
    for i, cw_id in enumerate(ids, 1):
        try:
            res = await _probe(db, cw_id)
        except Exception as exc:  # noqa: BLE001
            print(f"[{i}/{len(ids)}] {db.get(CatalogWork, cw_id).title!r}: ERROR {exc}")
            continue
        agg[res["verdict"]] += 1
        raw_total += res["raw"]
        t = res["top"]
        line = (f"[{i}/{len(ids)}] {res['verdict']:8} raw={res['raw']:3} {res['title'][:45]!r}")
        if t:
            line += f"  → conf={t['conf']} score={t['score']} seed={t['seeders']} {t['rel'][:55]!r}"
        if res.get("flags"):
            line += f"  ⚠ {','.join(res['flags'])}"
            flagged.append(res)
        print(line)
    total = len(ids)
    graded = agg["AUTO"] + agg["ACCEPTED"]
    print("\n" + "=" * 60)
    print(f"AUTO={agg['AUTO']} ACCEPTED={agg['ACCEPTED']} NO-RESULT={agg['NO-RESULT']}  "
          f"match-rate={graded}/{total} ({graded/total:.0%})  avg raw results={raw_total/total:.1f}")
    print(f"flagged top-picks (0-seeders / pack): {len(flagged)}")
    db.close()


if __name__ == "__main__":
    asyncio.run(main())
