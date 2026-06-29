"""Link each catalog (index) entry to its in-stock Work, ONCE, by matching titles against the files
actually on disk — so acquire/display never has to match at runtime, and a restore that remapped work
ids (mis-hooking catalog entries) is self-healed.

A catalog entry is hooked to an in-stock Work when its title HIGH-CONFIDENTLY and UNAMBIGUOUSLY matches
exactly one stocked file. Entries hooked to a crawled Work (no local file — a deliberate web-library
hook) are left alone; only file-backed hooks (the ones a restore could have corrupted) and un-hooked
entries are (re)derived. Conservative on purpose: a wrong link would point a reader at the wrong book,
so anything ambiguous is left to fetch.
"""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import CatalogGroup, CatalogWork, Work
from .extract import norm_title

log = logging.getLogger("shelf.stock")

_MIN_SCORE = 0.9   # segment-aware title score required to accept a catalog↔file link


def _title_keys(title: str) -> set[str]:
    """Coarse lookup keys for a title: the whole normalized title plus each clean segment (so a file
    'The Spellmonger Series: Book 02 - Warmage' and a catalog entry 'Warmage: Spellmonger, Book 2'
    share the key 'warmage')."""
    from . import verify
    out = {norm_title(title)}
    for seg in verify._segments(title):      # clean token-sets, series/collection segments excluded
        k = " ".join(sorted(seg))
        if k:
            out.add(k)
    return out


def link_catalog_to_stock(db: Session) -> dict:
    """Hook un-hooked / file-mis-hooked catalog GROUPS (and their member CatalogWorks) to the stocked
    Work that matches their title. Returns {linked, fixed, scanned}."""
    from . import matchmeta as mm
    from . import verify

    # 1) Index every on-disk (in-stock) Work by its title keys → [(work_id, title, media_kind)].
    files = db.execute(
        select(Work.id, Work.title, Work.media_kind)
        .where(Work.local_path.isnot(None), Work.local_path != "")
    ).all()
    if not files:
        return {"linked": 0, "fixed": 0, "scanned": 0}
    by_key: dict[str, list[tuple]] = {}
    work_titles: dict[int, str] = {}
    audio_work_ids: set[int] = set()
    for wid, title, mk in files:
        work_titles[wid] = title or ""
        if (mk or "") == "audio":
            # Audiobooks are a SEPARATE shared Work, surfaced as a title's "listen" format — they are
            # NEVER a catalog hook target (a CatalogGroup represents the ebook/comic). Hooking an ebook
            # entry to an audiobook Work (it has the same title) makes the title look acquired, but the
            # audio Work is hidden by the library view and the acquire short-circuit adds the WRONG work
            # to the user's library. Skip them as candidates; track their ids so a STALE audio hook is
            # corrected below rather than preserved (its title would otherwise match and be kept).
            audio_work_ids.add(wid)
            continue
        for key in _title_keys(title or ""):
            by_key.setdefault(key, []).append((wid, title or "", mk or "text"))

    # Which work ids are file-backed (so a hook pointing at one is a stock hook we may re-derive). Audio
    # ids are included: an audiobook mis-hook IS file-backed and must be re-derivable (→ corrected below).
    file_work_ids = set(work_titles)

    def _set_hook(g: CatalogGroup, wid: int | None) -> None:
        """Set the group's hook and roll it down to its member catalog rows (per-source index entries)."""
        g.hooked_work_id = wid
        db.query(CatalogWork).filter(CatalogWork.group_id == g.id).update(
            {CatalogWork.hooked_work_id: wid}, synchronize_session=False)

    linked = fixed = scanned = 0
    groups = db.scalars(select(CatalogGroup)).all()
    for g in groups:
        # Leave deliberate web-library hooks (hooked to a crawled, file-less Work) untouched.
        if g.hooked_work_id is not None and g.hooked_work_id not in file_work_ids:
            continue
        mis_audio = g.hooked_work_id in audio_work_ids   # wrongly hooked to an audiobook Work
        scanned += 1
        gt = g.title or ""
        # Gather candidate works that share any key with this title, then score precisely.
        cands: dict[int, tuple] = {}
        for key in _title_keys(gt):
            for wid, wt, mk in by_key.get(key, []):
                cands.setdefault(wid, (wt, mk))
        scored = []
        want_bucket = mm.bucket_of(None, media_kind=g.media_bucket)
        for wid, (wt, mk) in cands.items():
            s = verify._title_score(gt, wt)
            if want_bucket and mm.bucket_of(None, media_kind=mk) and \
                    mm.type_compat(want_bucket, mm.bucket_of(None, media_kind=mk)) < 0.5:
                continue                       # wrong medium (a comic file for a prose entry, etc.)
            scored.append((s, wid))
        scored.sort(reverse=True)
        accept = bool(scored and scored[0][0] >= _MIN_SCORE)
        # Unambiguous: the top match must clearly beat the runner-up (or be the only one).
        if accept and len(scored) > 1 and scored[1][0] >= _MIN_SCORE and scored[1][0] > scored[0][0] - 0.05:
            accept = False
        if not accept:
            if mis_audio:        # mis-hooked to an audiobook and no clear ebook on disk → UN-HOOK it
                _set_hook(g, None)
                fixed += 1
            continue
        best_wid = scored[0][1]
        if g.hooked_work_id == best_wid:
            continue
        was = g.hooked_work_id
        # Only OVERWRITE an existing file-hook when it's clearly WRONG (its work's title doesn't match
        # this entry) — so a restore-corrupted hook is corrected but a merely-lower-scored correct one is
        # never clobbered. An audiobook mis-hook (`mis_audio`) is ALWAYS wrong (its title matches yet the
        # medium is wrong), so it must be corrected regardless of the title score.
        if was is not None and not mis_audio and verify._title_score(gt, work_titles.get(was, "")) >= 0.5:
            continue
        _set_hook(g, best_wid)
        if was is None:
            linked += 1
        else:
            fixed += 1
    db.commit()
    log.info("catalog↔stock link: linked=%s fixed=%s (scanned=%s of %s groups)",
             linked, fixed, scanned, len(groups))
    return {"linked": linked, "fixed": fixed, "scanned": scanned}
