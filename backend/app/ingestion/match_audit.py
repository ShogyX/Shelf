"""Wrong-match detection over the ACQUIRED pool.

A "wrong match" is a Work whose FILE content doesn't belong to the title it's recorded (and
possibly catalog-hooked) as — the class of bug where a release-name match grabbed the wrong book
and post-download verification let it through. Ground truth is the file's EMBEDDED metadata
(EPUB OPF / PDF Info / audio album+artist tags), the same authority the import verifier uses.

``audit_work`` compares, for one Work:
  * embedded title/author  vs  Work.title/author        (the file vs what the library calls it)
  * Work.title/author      vs  each hooked CatalogWork  (what the library calls it vs the catalog)

and returns a suspect record when the title score is weak or two KNOWN authors are disjoint.
Evidence quality is graded: OPF/audio tags are authoritative; a filename-derived title (CBZ, tag-
less files) is weak — callers treat those as 'review' rather than 'wrong'.

Shared by the one-off audit scanner (scripts/match_audit_scan.py) and the periodic
``match_audit_tick`` watcher, so a wrong match that slips in later is flagged the same way.
"""
from __future__ import annotations

import logging
import os

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import CatalogWork, DownloadJob, Work
from . import verify
from .extract import authors_compatible, norm_title

log = logging.getLogger("shelf.match_audit")

# Below this embedded-vs-recorded title score a work is a SUSPECT (0.5 keeps subtitle/edition
# variants out of the list — "Mistborn" vs "Mistborn: The Final Empire" scores well above it).
SUSPECT_SCORE = 0.5
# At/above this the title alone clears the work even if the author strings look disjoint (author
# fields are messy: translators, narrators, "Various", initials-only).
CLEAR_SCORE = 0.9


def _embedded(work: Work) -> tuple[str | None, str | None, str]:
    """(title, author, evidence) from the FILE. evidence: 'tags' (audio), 'opf'/'pdf' (embedded),
    'filename' (weak — no embedded metadata, the name is all there is), 'none' (unreadable)."""
    path = work.local_path or ""
    if (work.media_kind or "") == "audio":
        meta = verify.read_audio_meta(path)
        if meta and meta.get("title"):
            return meta["title"], meta.get("author"), "tags"
        # Tag-less audiobook → the folder/file name is the only signal.
        base = os.path.basename(path.rstrip("/"))
        return (os.path.splitext(base)[0] or None), None, "filename"
    meta = verify.read_book_meta(path)
    if not meta:
        return None, None, "none"
    ext = (meta.get("fmt") or "").lower()
    stem = os.path.splitext(os.path.basename(path))[0]
    # read_book_meta falls back to the filename stem when there's no embedded title — grade that
    # honestly (a CBZ or a stripped EPUB can only ever be filename-checked).
    evidence = "filename" if (meta.get("title") or "") == stem else ("pdf" if ext == "pdf" else "opf")
    return meta.get("title"), meta.get("author"), evidence


# Album/title tags that are labels/junk, not the book's real title — NEVER adopt these as a
# corrected title, and never treat them as proof of wrong content (the audio may still be right).
_JUNK_TAG = None


def _junk_tag(t: str | None) -> bool:
    import re
    global _JUNK_TAG
    if _JUNK_TAG is None:
        _JUNK_TAG = re.compile(
            r"^(?:radio theatre|album|no title|disc \d+|cd ?\d+|track ?\d+|unknown|untitled"
            r"|https?://|created: |libri?vox weekly|bbc radio|audiobook|unabridged"
            r"|written by |read by |narrated by |various artists?$)", re.I)
    t = (t or "").strip()
    if not t or _JUNK_TAG.match(t):
        return True
    # Mojibake / control garbage (UTF-16-misread tags): too few plain letters to be a title.
    # ASCII letters only — mojibake like 'ÿþO' is all Unicode "letters" yet zero information.
    letters = sum(c.isascii() and c.isalpha() for c in t)
    return letters < 3


def usable_correction(title: str | None, author: str | None) -> tuple[str, str | None] | None:
    """(title, author) safe to ADOPT as a work's corrected identity from embedded tags, or None
    when the tags can't be trusted for a rename (junk/mojibake/label tags, swapped fields,
    narrator-in-author). Author prefixes like 'Written by' are stripped; a junk author doesn't
    block adoption of a clean title (author just stays unset)."""
    import re
    t = (title or "").strip()
    if not t or _junk_tag(t):
        return None
    a = (author or "").strip()
    a = re.sub(r"^(?:written|read|narrated)\s+by\s+", "", a, flags=re.I).strip()
    if a and (_junk_tag(a) or a.lower() == t.lower()
              or (re.search(r"\d", a) and "(" in a)):   # author field carrying a series/title
        a = ""
    return t[:512], (a[:255] or None)


def classify(work: Work, problem: dict, *, series: str | None = None) -> str:
    """Grade one suspect problem: 'ok' (explained false positive), 'wrong' (high-confidence wrong
    match), or 'review' (needs a human). The false-positive filters encode what the 2026-07 full-pool
    audit found: album tags routinely carry the SERIES name, slug/compacted tags contain the title,
    and label/junk tags ('Radio Theatre', mojibake) say nothing about the content."""
    import re
    if series is None:
        series = getattr(work, "series", None)   # default from the work — call sites can't forget it

    def compact(s: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", norm_title(s or ""))

    if problem["kind"] == "file_vs_work":
        et = problem.get("embedded_title") or ""
        if _junk_tag(et):
            return "review"                              # tags junk → content unverifiable here
        if series and norm_title(series) == norm_title(et):
            return "ok"                                  # album tag = the series name
        cw_, ce = compact(work.title or ""), compact(et)
        if ce and cw_ and (cw_ in ce or (len(ce) >= 5 and ce in cw_)):
            return "ok"                                  # slug/compacted containment either way
        if problem["score"] == 0 and not problem["authors_ok"]:
            return "wrong"                               # different title AND different author
        return "review"                                  # same author or partial overlap → human call
    # work_vs_hook
    if problem["score"] == 0 and not problem["authors_ok"]:
        return "wrong"                                   # unrelated catalog row hooked
    return "review"                                      # alt-title/translation rows land here


def audit_work(db: Session, work: Work) -> dict | None:
    """Suspect record for ``work``, or None when the match looks right (or can't be assessed —
    missing files are the integrity scan's job, not a match problem)."""
    path = work.local_path or ""
    if not path or not os.path.exists(path):
        return None
    emb_title, emb_author, evidence = _embedded(work)
    problems: list[dict] = []

    if emb_title:
        score = verify._title_score(work.title or "", emb_title)
        authors_ok = authors_compatible(work.author, emb_author)
        if score < SUSPECT_SCORE or (not authors_ok and score < CLEAR_SCORE):
            problems.append({
                "kind": "file_vs_work", "score": round(score, 3), "authors_ok": authors_ok,
                "embedded_title": emb_title, "embedded_author": emb_author,
            })

    # The catalog rows this work fulfils: a hook to a DIFFERENT logical title is a wrong match
    # even when the file matches the Work row (the hook is what search/acquire trust).
    for cw in db.scalars(select(CatalogWork).where(CatalogWork.hooked_work_id == work.id)).all():
        score = verify._title_score(cw.title or "", work.title or "")
        authors_ok = authors_compatible(cw.author, work.author)
        if score < SUSPECT_SCORE or (not authors_ok and score < CLEAR_SCORE):
            problems.append({
                "kind": "work_vs_hook", "score": round(score, 3), "authors_ok": authors_ok,
                "catalog_id": cw.id, "catalog_title": cw.title, "catalog_author": cw.author,
                "provider": cw.provider, "domain": cw.domain,
            })

    if not problems:
        return None
    # Provenance: how this file arrived (the acquisition path a wrong match came through).
    job = db.scalars(select(DownloadJob).where(DownloadJob.work_id == work.id)
                     .order_by(DownloadJob.id.desc()).limit(1)).first()
    return {
        "work_id": work.id, "title": work.title, "author": work.author,
        "media_kind": work.media_kind, "language": work.language, "path": path,
        "norm_key": norm_title(work.title or ""), "evidence": evidence,
        "problems": problems,
        "provenance": ({"grab_kind": job.grab_kind, "release_title": job.release_title,
                        "job_title": job.title, "indexer": job.indexer} if job else None),
    }
