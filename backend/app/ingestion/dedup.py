"""Language-aware library dedup: keep at most ONE Work per (normalized title, author, media_kind,
language-bucket) — so an English AND a Norwegian edition of a title coexist for BOTH ebook and
audiobook, but a second same-language same-format copy is a duplicate. Also collapses byte-identical
copies (same content_hash) regardless of title.

The keeper per group is the catalog-hooked copy > most library members > best format > has cover >
oldest. Losers have their memberships migrated + catalog/index hooks RE-POINTED to the keeper (never
nulled — nulling makes the catalog re-grab the "missing" book), then are purged (refs + file).

Runs on a schedule (conservative: content_hash + exact same-edition only) with a per-run cap so a
mis-classification can't cascade into a mass delete. Reused by an on-demand admin sweep too.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
from collections import defaultdict

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ..library import purge_work
from ..models import CatalogGroup, CatalogWork, IndexedPage, LibraryItem, QueuedHook, Work
from .extract import norm_title
from . import language

log = logging.getLogger("shelf.dedup")

# Reading-format preference for the keeper (higher = kept). Audiobooks compared among themselves.
_FMT_RANK = {".epub": 5, ".azw3": 4, ".azw": 4, ".mobi": 3, ".pdf": 2, ".txt": 1, ".md": 1,
             ".cbz": 2, ".cbr": 1, ".m4b": 3, ".m4a": 2, ".flac": 2, ".mp3": 1, ".ogg": 1}
_MAX_PRUNE_PER_RUN = 200   # backstop: a bug can't cascade into a mass delete


def _ext(w: Work) -> str:
    return os.path.splitext(w.local_path or "")[1].lower()


def edition_key(w: Work) -> tuple:
    """The identity we keep ONE Work for: title + author + format + language bucket. Two Works with
    the same key are duplicates; differing language (en vs no) or media_kind keeps them distinct."""
    return (norm_title(w.title or ""), (w.author or "").strip().lower(),
            w.media_kind or "text", language.bucket(w.language))


def edition_exists(db: Session, *, title: str | None, author: str | None,
                   media_kind: str, lang: str | None) -> bool:
    """True if the library ALREADY holds this edition — same normalized title, author-compatible,
    same format, same language bucket. The download tracker calls this to avoid importing a duplicate
    beyond the one-English-plus-one-Norwegian-per-format rule (a Norwegian edition is NOT a duplicate
    of the English one, so both are kept)."""
    nt = norm_title(title or "")
    if not nt:
        return False
    lb = language.bucket(lang)
    ta = set(re.findall(r"[a-z]+", (author or "").lower()))
    for w in db.scalars(select(Work).where(
            Work.media_kind == media_kind, Work.local_path.is_not(None))).all():
        if norm_title(w.title or "") != nt or language.bucket(w.language) != lb:
            continue
        tb = set(re.findall(r"[a-z]+", (w.author or "").lower()))
        if not ta or not tb or (ta & tb):      # author-compatible (unknown author doesn't block)
            return True
    return False


def _groups(works: list[Work]) -> list[list[Work]]:
    """Duplicate groups: union of identical bytes (content_hash) and identical edition_key. Works
    with no title/author are only grouped by content_hash (title-less identity is unreliable)."""
    parent: dict[int, int] = {}

    def find(x: int) -> int:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    by_hash: dict[str, list[int]] = defaultdict(list)
    by_edition: dict[tuple, list[int]] = defaultdict(list)
    wmap = {w.id: w for w in works}
    for w in works:
        find(w.id)
        if w.content_hash:
            by_hash[w.content_hash].append(w.id)
        k = edition_key(w)
        if k[0] and k[1]:                     # require title AND author for edition grouping
            by_edition[k].append(w.id)
    for ids in list(by_hash.values()) + list(by_edition.values()):
        for i in ids[1:]:
            union(ids[0], i)
    grouped: dict[int, list[Work]] = defaultdict(list)
    for w in works:
        grouped[find(w.id)].append(w)
    return [g for g in grouped.values() if len(g) > 1]


def run(db: Session, *, execute: bool = False, cap: int = _MAX_PRUNE_PER_RUN) -> dict:
    """Find + (if execute) prune duplicate Works. Returns stats. Read-only when execute=False."""
    works = [w for w in db.scalars(select(Work)).all() if w.local_path]
    memb = dict(db.execute(select(LibraryItem.work_id, func.count()).group_by(LibraryItem.work_id)).all())
    hooked = set(db.scalars(
        select(CatalogWork.hooked_work_id).where(CatalogWork.hooked_work_id.is_not(None))).all())

    def score(w: Work) -> tuple:
        return (1 if w.id in hooked else 0, memb.get(w.id, 0), _FMT_RANK.get(_ext(w), 0),
                1 if w.cover_url else 0, -w.id)

    groups = _groups(works)
    loser_ids = {w.id for g in groups for w in g if w.id != max(g, key=score).id}
    alive = {w.local_path for w in works if w.id not in loser_ids}
    stats = {"groups": len(groups), "pruned": 0, "migrated": 0, "repointed": 0, "capped": False}
    if not execute:
        stats["would_prune"] = len(loser_ids)
        return stats

    for g in groups:
        if stats["pruned"] >= cap:
            stats["capped"] = True
            log.warning("dedup: per-run cap %d hit — leaving the rest for the next run", cap)
            break
        keeper = max(g, key=score)
        for w in g:
            if w.id == keeper.id or stats["pruned"] >= cap:
                continue
            for li in db.scalars(select(LibraryItem).where(LibraryItem.work_id == w.id)).all():
                if not db.scalar(select(LibraryItem.id).where(
                        LibraryItem.user_id == li.user_id, LibraryItem.work_id == keeper.id)):
                    db.add(LibraryItem(user_id=li.user_id, work_id=keeper.id, added_at=li.added_at,
                                       auto_kindle_through=li.auto_kindle_through))
                    stats["migrated"] += 1
            db.flush()
            for tbl, col in ((CatalogWork, "hooked_work_id"), (CatalogGroup, "hooked_work_id"),
                             (IndexedPage, "hooked_work_id"), (QueuedHook, "related_work_id"),
                             (QueuedHook, "hooked_work_id")):
                stats["repointed"] += db.execute(
                    update(tbl).where(getattr(tbl, col) == w.id).values(**{col: keeper.id})).rowcount or 0
            db.flush()
            path = w.local_path
            purge_work(db, w)
            _safe_delete(path, alive)
            stats["pruned"] += 1
    if stats["pruned"]:
        log.info("dedup: pruned %d duplicate Work(s) (migrated %d memberships, repointed %d hooks)",
                 stats["pruned"], stats["migrated"], stats["repointed"])
    return stats


def _safe_delete(path: str | None, alive: set[str]) -> None:
    """Delete a pruned loser's file/folder — but NEVER a path an alive Work uses or lives under
    (shared audiobook folders)."""
    if not path or path in alive:
        return
    base = path.rstrip("/")
    if any(a == path or a.startswith(base + "/") for a in alive):
        return
    try:
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
        elif os.path.isfile(path):
            os.remove(path)
            d = os.path.dirname(path)
            if os.path.isdir(d) and not os.listdir(d):
                os.rmdir(d)
    except OSError:
        log.info("dedup: could not remove %s", path, exc_info=True)
