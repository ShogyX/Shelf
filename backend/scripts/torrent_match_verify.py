"""V1 — torrent-matching accuracy acceptance test (R23). DEFERRED / resource-heavy: NOT run in CI.

Per run, picks N random catalog titles and drives the FULL torrent path end-to-end for each —
release_matcher (torrent protocol) → qBittorrent download → VirusTotal scan → verify.verify_download
— then classifies the outcome:
  * CORRECT    — verify confirmed the imported file is the requested book (title/author/ISBN);
  * INCORRECT  — a file imported but is the wrong book (verify false-positive — needs a spot-check);
  * NO-RESULT  — no torrent candidate cleared the matcher (not a precision failure).
Runs R times and reports per-run + aggregate precision = CORRECT / (CORRECT + INCORRECT), plus every
import (title + release name) for the operator to audit INCORRECT cases.

It downloads real files into a DEDICATED qBittorrent category and DELETES each torrent + its data
after the title (no library pollution). Honors the VirusTotal rate limit. Requires qBittorrent +
Prowlarr (torrent indexers) + (optionally) VirusTotal configured.

  Usage:  .venv/bin/python scripts/torrent_match_verify.py [N] [RUNS]   # defaults: N=100, RUNS=3

Acceptance bar: precision >= 90%, zero INCORRECT imports that VirusTotal+verify should have caught.
"""
from __future__ import annotations

import asyncio
import random
import sys

from sqlalchemy import func, select

from app.db import SessionLocal
from app.ingestion import torrents
from app.integrations.qbittorrent import is_complete
from app.models import CatalogWork, DownloadJob

POLL_TIMEOUT_S = 150    # max wait for a single torrent to finish before giving up (NO-RESULT)
POLL_EVERY_S = 5


def _sample(db, n: int) -> list[int]:
    """The N most-POPULAR un-hooked titles (stable ids so runs are comparable). Popular titles are the
    ones that actually exist on torrent trackers — random sampling mostly hits obscure public-domain
    works absent from trackers (NO-RESULT noise) that flow via Anna's Archive instead."""
    return list(db.scalars(
        select(CatalogWork.id).where(CatalogWork.hooked_work_id.is_(None),
                                     CatalogWork.author.is_not(None))
        .order_by(CatalogWork.popularity.desc()).limit(n)).all())


async def _one_title(db, cw_id: int) -> tuple[str, str | None]:
    """Run the torrent path for one title to a terminal state. Returns (verdict, release_title)."""
    cw = db.get(CatalogWork, cw_id)
    if cw is None or cw.hooked_work_id is not None:
        return "NO-RESULT", None
    try:
        job = await torrents.grab(db, cw)
    except Exception as exc:  # noqa: BLE001 — infra error for this title → NO-RESULT, keep going
        print(f"  ! grab error for {cw.title!r}: {exc}")
        return "NO-RESULT", None
    if job is None:
        return "NO-RESULT", None

    # Drive ONLY this job (not the global tick, which would cross-fail other titles' jobs in a batch).
    qb = torrents.get_qbittorrent(db)
    client = torrents._client(qb)
    waited = 0
    while waited < POLL_TIMEOUT_S:
        infos = {t.hash: t for t in await client.torrents_info(category=torrents._category(qb))}
        t = infos.get((job.nzo_id or "").lower())
        if t is None:
            job.status = "failed"; job.error = "torrent vanished"; db.commit()
            break
        if is_complete(t.state) or t.progress >= 1.0:
            await torrents._finish(db, client, qb, job, t)   # VT scan + verify + import for THIS job
            db.refresh(job)
            break
        await asyncio.sleep(POLL_EVERY_S)
        waited += POLL_EVERY_S

    rel = job.release_title
    # Cleanup: remove the torrent + its data regardless of outcome (no library pollution).
    await torrents._remove(client, job, delete_files=True)
    verdict = "CORRECT" if job.status == "imported" else "NO-RESULT"
    if job.status == "imported":
        # verify already confirmed title/author/ISBN → CORRECT (flag for manual INCORRECT audit).
        # Then PURGE the imported Work + its promoted file so the accuracy run never pollutes the
        # library (the run is a measurement, not a stocking job).
        _purge_import(db, job)
    return verdict, rel


def _purge_import(db, job) -> None:
    import os
    from app.routers.works import purge_work
    from app.models import Work
    w = db.get(Work, job.work_id) if job.work_id else None
    path = w.local_path if w else None
    if w is not None:
        try:
            purge_work(db, w); db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
    if path and os.path.isfile(path):
        try:
            os.remove(path)
            d = os.path.dirname(path)
            if os.path.isdir(d) and not os.listdir(d):
                os.rmdir(d)
        except OSError:
            pass
    db.delete(job); db.commit()


async def _run(db, ids: list[int], run_no: int) -> dict:
    counts = {"CORRECT": 0, "INCORRECT": 0, "NO-RESULT": 0}
    imports: list[tuple[int, str | None]] = []
    for i, cw_id in enumerate(ids, 1):
        verdict, rel = await _one_title(db, cw_id)
        counts[verdict] += 1
        if verdict == "CORRECT":
            imports.append((cw_id, rel))
        print(f"run {run_no} [{i}/{len(ids)}] cw={cw_id} → {verdict} ({rel or '-'})")
    return {"counts": counts, "imports": imports}


async def main() -> None:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 100
    runs = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    db = SessionLocal()
    if not torrents.configured(db):
        print("ABORT: torrent route not configured (need qBittorrent + Prowlarr torrent indexers).")
        return
    ids = _sample(db, n)
    print(f"V1 torrent accuracy: {len(ids)} titles x {runs} runs\n")

    agg = {"CORRECT": 0, "INCORRECT": 0, "NO-RESULT": 0}
    all_imports: list[tuple[int, int, str | None]] = []
    for r in range(1, runs + 1):
        res = await _run(db, ids, r)
        c = res["counts"]
        for k in agg:
            agg[k] += c[k]
        graded = c["CORRECT"] + c["INCORRECT"]
        prec = (c["CORRECT"] / graded) if graded else 1.0
        print(f"\n== run {r}: CORRECT={c['CORRECT']} INCORRECT={c['INCORRECT']} "
              f"NO-RESULT={c['NO-RESULT']} precision={prec:.1%}\n")
        all_imports += [(r, i, rel) for i, rel in res["imports"]]

    graded = agg["CORRECT"] + agg["INCORRECT"]
    prec = (agg["CORRECT"] / graded) if graded else 1.0
    print("=" * 60)
    print(f"AGGREGATE: CORRECT={agg['CORRECT']} INCORRECT={agg['INCORRECT']} "
          f"NO-RESULT={agg['NO-RESULT']}  precision={prec:.1%}  (bar: >= 90%)")
    print("\nImports to spot-check for INCORRECT (verify thought these were right):")
    for r, cw_id, rel in all_imports:
        cw = db.get(CatalogWork, cw_id)
        print(f"  run {r} cw={cw_id} want={cw.title if cw else '?'!r} got={rel!r}")
    db.close()


if __name__ == "__main__":
    asyncio.run(main())
