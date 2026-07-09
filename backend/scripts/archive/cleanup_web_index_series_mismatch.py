"""One-off cleanup for the web-novel-vs-metadata source-matching bug.

A novel-only crawl source (web_index, e.g. novellunar) was wrongly matched/tagged onto titles that
originate from a metadata provider (hardcover/openlibrary/googlebooks), because:
  * acquire's web_index gate skipped the author check when the picked metadata rep was AUTHORLESS
    (a googlebooks "Necromancer" with no author) — so a same-title web novel got hooked; and
  * series persistence (_apply_series_rows) tags EVERY catalog row sharing a normalized title with
    the series name, with no author check — so same-title different-author crawl rows got the wrong
    extra.series / series_position.

This un-tags / un-hooks the PROVABLY-WRONG web_index rows. A row is provably wrong only when the
series has a known canonical metadata author and:
  * (hooked) the hooked WORK's author is incompatible with that canonical author — the work is
    genuinely a different book than the series claims (e.g. Diana Gabaldon's "Voyager" tagged
    "Sherlock Holmes", or "Necromancer" by "Pig On A Journey" tagged "The Spellmonger"); or
  * (tag-only) an unhooked listing row whose OWN author is incompatible with the canonical author.

It NEVER touches a row whose underlying work genuinely matches the series (e.g. a GoT scanlation
listing correctly hooked to GRRM's "A Game of Thrones"), and it NEVER deletes a Work, a LibraryItem,
or a catalog row — it only clears the wrong series identity (extra.series/series_position/series_id),
the wrong hooked_work_id mismatch, and a wrong series tag stamped on the Work itself.

Run with no args for a DRY RUN (prints the plan). Run with --commit to apply (writes an undo file).
"""
from __future__ import annotations

import json
import sys
import time
from collections import Counter

from sqlalchemy import select

from app.db import SessionLocal
from app.ingestion.extract import _author_norm
from app.models import CatalogWork, Work

META_PROVIDERS = ("hardcover", "openlibrary", "googlebooks")
UNDO_PATH = f"cleanup_web_index_series_mismatch_undo_{int(time.time())}.json"


def _tokset(author: str | None) -> set[str]:
    return set(_author_norm(author).split()) if author else set()


def _canon_tokens(db, series: str) -> set[str] | None:
    """Token set of the series' DOMINANT metadata author (tokens present in a strict majority of the
    series' metadata-provider rows). None when the series has no metadata author at all (can't judge)."""
    authors = db.scalars(
        select(CatalogWork.author).where(
            CatalogWork.provider.in_(META_PROVIDERS),
            CatalogWork.author.is_not(None),
            CatalogWork.extra["series"].as_string() == series,
        )
    ).all()
    if not authors:
        return None
    tok: Counter[str] = Counter()
    for a in authors:
        for t in _tokset(a):
            tok[t] += 1
    n = len(authors)
    return {t for t, ct in tok.items() if ct >= max(2, (n + 1) // 2)} or None


def main() -> None:
    commit = "--commit" in sys.argv[1:]
    db = SessionLocal()

    # Every web_index catalog row that carries a series tag.
    rows = db.scalars(
        select(CatalogWork).where(
            CatalogWork.provider == "web_index",
            CatalogWork.extra["series"].as_string().is_not(None),
        )
    ).all()

    canon_cache: dict[str, set[str] | None] = {}
    cat_tag_clears: list[tuple[CatalogWork, str]] = []      # (row, series) — strip extra.series tag
    cat_unhooks: list[tuple[CatalogWork, int, str]] = []    # (row, wid, work_title) — clear hooked_work_id
    work_series_clears: dict[int, Work] = {}                # wid -> Work whose wrong series tag to clear
    undo: dict = {"catalog": [], "works": []}

    for row in rows:
        ex = dict(row.extra or {})
        series = ex.get("series")
        if not series:
            continue
        if series not in canon_cache:
            canon_cache[series] = _canon_tokens(db, series)
        canon = canon_cache[series]
        if not canon:
            continue  # no metadata author known for this series → can't prove the row wrong

        work = db.get(Work, row.hooked_work_id) if row.hooked_work_id else None
        if work is not None:
            wtok = _tokset(work.author)
            if not wtok or (wtok & canon):
                continue  # the underlying work genuinely matches the series → leave it untouched
            # The hooked work is a DIFFERENT book than the series claims → wrong.
            cat_tag_clears.append((row, series))
            rtok = _tokset(row.author)
            if not (rtok and (rtok & wtok)):
                # The catalog row's own author also disagrees with its work → a genuine row→work
                # mishook (e.g. a "Sherlock Holmes / Doyle" listing hooked to Gabaldon's Voyager).
                # Clear the hooked_work_id. (When the row DOES represent its own work — e.g. cw154898
                # "Pig On A Journey" == its work — keep the hook; only the series tag was wrong.)
                cat_unhooks.append((row, work.id, work.title))
            # If the wrong series got stamped onto the Work itself, clear it there too.
            if work.series == series:
                work_series_clears[work.id] = work
        else:
            rtok = _tokset(row.author)
            if rtok and not (rtok & canon):
                cat_tag_clears.append((row, series))  # tag-only listing row with incompatible author

    # ---- report ----
    print(f"web_index rows carrying a series tag: {len(rows)}")
    print(f"catalog rows to STRIP series tag:     {len(cat_tag_clears)}")
    print(f"  of those, also CLEAR hooked_work_id: {len(cat_unhooks)}")
    print(f"works to clear a WRONG series tag:     {len(work_series_clears)}\n")

    print("-- catalog rows (id, series→cleared, pos, row_author, hooked_work_id, action) --")
    unhook_ids = {r.id for r, _w, _t in cat_unhooks}
    for row, series in sorted(cat_tag_clears, key=lambda x: x[0].id):
        pos = (row.extra or {}).get("series_position")
        action = "unhook+untag" if row.id in unhook_ids else (
            "untag(keep hook)" if row.hooked_work_id else "untag(listing)")
        print(f"   cw{row.id:>7}  {series!r:42}  pos={pos}  author={row.author!r}  "
              f"hooked={row.hooked_work_id}  -> {action}")
    print("\n-- works to clear wrong series tag --")
    for w in work_series_clears.values():
        print(f"   work{w.id:>6}  {w.title!r:42}  author={w.author!r}  series={w.series!r} -> None")

    if not commit:
        print("\nDRY RUN — re-run with --commit to apply.")
        db.close()
        return

    # ---- apply ----
    for row, series in cat_tag_clears:
        ex = dict(row.extra or {})
        undo["catalog"].append({"id": row.id, "extra": json.loads(json.dumps(ex)),
                                "hooked_work_id": row.hooked_work_id})
        for k in ("series", "series_position", "series_id"):
            ex.pop(k, None)
        row.extra = ex
    for row, _wid, _title in cat_unhooks:
        row.hooked_work_id = None
    for w in work_series_clears.values():
        undo["works"].append({"id": w.id, "series": w.series,
                              "series_position": w.series_position, "series_id": w.series_id})
        w.series = None
        w.series_position = None
        w.series_id = None
    db.commit()

    with open(UNDO_PATH, "w") as fh:
        json.dump(undo, fh, indent=2)
    print(f"\nCOMMITTED. Undo snapshot written to {UNDO_PATH} "
          f"({len(undo['catalog'])} catalog rows, {len(undo['works'])} works).")
    db.close()


if __name__ == "__main__":
    main()
