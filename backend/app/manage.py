"""shelfmanage — small library-maintenance commands.

The operations here are ones the web UI deliberately has no button for, because they are rare,
per-title, and destructive enough to want a considered decision — but which otherwise force an
operator into hand-written SQL against the live database. That is the failure mode this module
exists to remove: an ad-hoc ``UPDATE works SET …`` has no dry run, no guard, no test, and one typo'd
WHERE clause is an incident. These do the same jobs with a stated plan, a confirmation, and the
app's own code paths (``library.purge_work`` rather than raw DELETEs, so back-pointers are cleaned).

Every command prints what it WOULD do and changes nothing unless ``--yes`` is passed.

    shelfmanage adopt-audiobook "/media/Audiobooks/Winter's Heart" --author "Robert Jordan"
    shelfmanage repoint-work 6832 "/media/Audiobooks/Towers of Midnight"
    shelfmanage remove-work 2752
    shelfmanage list-broken
"""
from __future__ import annotations

import argparse
import os
import sys

from sqlalchemy import select

from .db import SessionLocal
from .models import Source, Work


def _fail(msg: str) -> int:
    print(f"shelfmanage: {msg}", file=sys.stderr)
    return 2


def _confirm(args, plan: str) -> bool:
    """Print the plan; act only on --yes. A dry run is the default because these are the commands
    you reach for while tired, on the live library."""
    print(plan)
    if not args.yes:
        print("\n(dry run — nothing changed. Pass --yes to apply.)")
        return False
    return True


# --------------------------------------------------------------------------- adopt-audiobook
def _audio_files(folder: str) -> list[str]:
    exts = (".mp3", ".m4b", ".m4a", ".flac", ".ogg", ".opus", ".wma", ".aac")
    return sorted(f for f in os.listdir(folder)
                  if f.lower().endswith(exts) and os.path.getsize(os.path.join(folder, f)) > 0)


def cmd_adopt_audiobook(args) -> int:
    """Create a Work for an audiobook that is already on disk.

    Audiobook Works are otherwise only ever created by the download-import path, so a folder that
    arrives any other way — restored from a backup, rescued out of a mis-imported collection, copied
    in by hand — is invisible: the library is per-user and the frontend has no way to adopt an
    existing folder. Mirrors what ``import_core._import_audiobook`` builds (same source, same
    ``source_work_ref`` shape) so the result is indistinguishable from a normally-imported book."""
    from .covers import save_cover
    from .ingestion import language, verify
    from .ingestion.extract import norm_title

    folder = os.path.abspath(args.folder.rstrip("/"))
    if not os.path.isdir(folder):
        return _fail(f"not a folder: {folder}")
    files = _audio_files(folder)
    if not files:
        return _fail(f"no playable audio files in {folder}")

    # Prefer the embedded tags over the folder name: scene/NZB folder names are frequently mangled,
    # and the tags are the content's own claim about itself.
    meta = verify.read_audio_meta(folder) or {}
    title = args.title or meta.get("title") or os.path.basename(folder)
    author = args.author or (meta.get("author") if meta.get("author_field") == "album_artist" else None)

    db = SessionLocal()
    try:
        ref = f"audiobook:std:{norm_title(title)}" or f"audiobook:path:{folder}"
        clash = db.scalar(select(Work).where(Work.source_work_ref == ref))
        if clash is not None:
            print(f"already adopted: work {clash.id} {clash.title!r} → {clash.local_path}")
            return 0
        existing = db.scalar(select(Work).where(Work.local_path == folder))
        if existing is not None:
            print(f"already adopted: work {existing.id} {existing.title!r}")
            return 0

        plan = [
            "adopt audiobook",
            f"  folder : {folder}",
            f"  files  : {len(files)} playable",
            f"  title  : {title}",
            f"  author : {author or '(unknown)'}",
            f"  series : {args.series or '(none)'}",
        ]
        if args.series_position is not None:
            plan.append(f"  pos    : {args.series_position:g}")
        if not _confirm(args, "\n".join(plan)):
            return 0

        src = db.scalar(select(Source).where(Source.key == "local")) or db.scalar(select(Source))
        work = Work(source_id=src.id if src else None, source_work_ref=ref, title=title,
                    author=author, media_kind="audio", status="complete", local_path=folder,
                    language=language.detect_text_language(title, min_tokens=2) or "en",
                    hooked=False, series=args.series, series_position=args.series_position)
        db.add(work)
        db.commit()
        db.refresh(work)
        art = verify.read_audio_cover(folder)
        if art:
            work.cover_url = save_cover(f"audio-{work.id}", art[0], art[1])
            db.commit()
        print(f"created work {work.id}: {work.title!r}"
              f"{' with cover' if work.cover_url else ''}")
        return 0
    finally:
        db.close()


# ------------------------------------------------------------------------------ repoint-work
def cmd_repoint_work(args) -> int:
    """Point a Work at a different path on disk, and drop what was cached about the old one.

    For when a Work's files move, or when it was hooked to the wrong folder in the first place.
    Clearing ``audio_meta`` matters as much as the path: it caches the probed track list, so a Work
    left pointing at a folder of 1,582 tracks keeps offering all 1,582 until the cache is dropped."""
    path = os.path.abspath(args.path.rstrip("/"))
    if not os.path.exists(path):
        return _fail(f"path does not exist: {path}")
    db = SessionLocal()
    try:
        work = db.get(Work, args.work_id)
        if work is None:
            return _fail(f"no work with id {args.work_id}")
        if not _confirm(args, f"repoint work {work.id} {work.title!r}\n"
                              f"  from : {work.local_path}\n"
                              f"  to   : {path}\n"
                              f"  also : clear cached audio manifest + health, so both re-derive"):
            return 0
        work.local_path = path
        work.audio_meta = None
        work.health, work.health_detail, work.health_checked_at = "ok", None, None
        db.commit()
        print(f"work {work.id} now points at {path}")
        return 0
    finally:
        db.close()


# ------------------------------------------------------------------------------- remove-work
def cmd_remove_work(args) -> int:
    """Delete a Work and every back-pointer to it (a duplicate, or a broken import).

    Uses ``library.purge_work``, which is the same routine the watched-folder sync uses when a file
    disappears — memberships, shelf placements, crawl jobs, metadata links and the catalog "hooked"
    pointers all go with it. SQLite FK enforcement is off here, so a hand-written DELETE would leave
    every one of those dangling."""
    from .library import purge_work

    db = SessionLocal()
    try:
        work = db.get(Work, args.work_id)
        if work is None:
            return _fail(f"no work with id {args.work_id}")
        shared = db.scalars(select(Work.id).where(Work.local_path == work.local_path,
                                                  Work.id != work.id)).all() if work.local_path else []
        files_note = ("  files  : KEPT on disk" if not args.delete_files else
                      f"  files  : DELETE {work.local_path}"
                      + (f"  (refused — also used by work(s) {list(shared)})" if shared else ""))
        if not _confirm(args, f"remove work {work.id} {work.title!r}\n"
                              f"  kind   : {work.media_kind}\n"
                              f"  path   : {work.local_path}\n"
                              f"{files_note}\n"
                              f"  and    : memberships, shelves, jobs, metadata links, hooks"):
            return 0
        purge_work(db, work, delete_files=args.delete_files)
        db.commit()
        print(f"removed work {args.work_id}")
        return 0
    finally:
        db.close()


# ------------------------------------------------------------------------------- heal-hooks
# Structural words that say nothing about WHICH book this is. Volume/ordinal words matter most:
# "Heartstopper: Volume Four" and "Skysworn: Cradle: Volume Four" share only "volume four", and
# without stripping those a shared-word test reads them as the same title.
_NOISE = {
    "the", "a", "an", "of", "and", "in", "on", "at", "to", "for", "with", "from", "by", "is", "it",
    "or", "de", "la", "le", "el", "und", "der", "die", "das",
    "volume", "vol", "book", "part", "novel", "edition", "series", "complete", "unabridged",
    "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
    "first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth", "ninth", "tenth",
}


def _significant(title: str | None) -> set[str]:
    """The words in a title that actually identify the work."""
    from .ingestion.extract import norm_title
    return {w for w in norm_title(title or "").split() if w not in _NOISE and len(w) > 2}


def _hook_is_wrong(row_title: str | None, work_title: str | None,
                   work_path: str | None = None) -> bool:
    """Whether a hook is confidently wrong — deliberately CONSERVATIVE.

    Clearing a correct hook makes an owned title look unacquired, so this only fires when the two
    titles share no identifying word at all. It therefore UNDER-clears on purpose: near-miss
    mis-hooks that happen to share one ("Zodiac Academy: The Awakening" vs "Zodiac Academy: Fated
    Throne") survive and are left for a human. Editions, spelling and subtitle drift ("Dubliners" vs
    "Dubliners (Oxford World's Classics)", "Dr." vs "Doctor") must never be touched.

    KNOWN residual false positive: a Latin-script TRANSLATION whose title shares no word with the
    original ("Das Parfum" vs "Perfume: The Story of a Murderer") reads as unrelated. Language can't
    separate those — the stored values are inconsistent ("en"/"English"/"EN-US") and the app's
    bucket() folds every one of them, French and German included, to "en". Measured at roughly 1 in
    170, and the cost is only that a card shows as un-acquired, so it is accepted rather than
    hand-listed."""
    from .ingestion.extract import is_latin_title
    a, b = _significant(row_title), _significant(work_title)
    if not a or not b:
        return False          # nothing to judge on → leave it alone
    if not (is_latin_title(row_title or "") and is_latin_title(work_title or "")):
        # Cross-script pairs can't be judged by word overlap — a Greek or Japanese edition row
        # legitimately shares no word with its English work. Only 1 hook in this library is in that
        # position, and spuriously clearing a real translation is worse than leaving it.
        return False
    if a & b:
        return False
    # The Work's TITLE is not always the book's title — plenty are filename-ish stubs like
    # "Ian Fleming - Bond 1" or "L. Frank Baum_Oz 03", whose hook from "Casino Royale" / "Ozma of Oz"
    # is perfectly correct. The path usually still carries the real title, so check it before
    # concluding anything: ".../Casino Royale/James Bond 01 - Ian Fleming - Casino Royale.mobi".
    if a & _significant((work_path or "").replace("/", " ").replace("_", " ").replace("-", " ")):
        return False
    return True


def cmd_heal_hooks(args) -> int:
    """Clear catalog hooks that point at an unrelated work, so they re-enter the normal fetch path.

    A group's hook used to be rolled down onto every member row regardless of its own title, so one
    bad cluster membership pointed unrelated titles at the wrong book — "War and Peace" resolving to
    "One Piece", "Harry Potter" to "Peter Pan in Kensington Gardens". stock_link now verifies each
    member before it inherits a hook, but rows mis-hooked BEFORE that fix keep their stale value
    until something clears it. This is that something; it is re-runnable and safe to repeat."""
    from .models import CatalogGroup, CatalogWork, Work

    db = SessionLocal()
    try:
        works = {wid: (title, path)
                 for wid, title, path in db.execute(select(Work.id, Work.title, Work.local_path))}
        rows = db.scalars(select(CatalogWork).where(CatalogWork.hooked_work_id.is_not(None))).all()
        groups = db.scalars(select(CatalogGroup).where(CatalogGroup.hooked_work_id.is_not(None))).all()
        def _wrong(obj) -> bool:
            title, path = works.get(obj.hooked_work_id) or (None, None)
            return _hook_is_wrong(obj.title, title, path)

        bad_rows = [r for r in rows if _wrong(r)]
        bad_groups = [g for g in groups if _wrong(g)]
        if not bad_rows and not bad_groups:
            print("no wrong hooks found.")
            return 0

        plan = [f"clear {len(bad_rows)} catalog row hook(s) and {len(bad_groups)} card hook(s)",
                f"  scanned : {len(rows)} row hooks, {len(groups)} card hooks",
                "  effect  : those titles show as un-acquired and re-enter the normal fetch/hook path",
                "  safety  : only hooks sharing NO identifying word with their target are touched"]
        for g in bad_groups[:args.show]:
            plan.append(f"    card {g.id:>7} {(g.title or '')[:36]:38} -> "
                        f"{((works.get(g.hooked_work_id) or ('', ''))[0] or '')[:30]}")
        if len(bad_groups) > args.show:
            plan.append(f"    ... and {len(bad_groups) - args.show} more card(s)")
        if not _confirm(args, "\n".join(plan)):
            return 0

        for r in bad_rows:
            r.hooked_work_id = None
        for g in bad_groups:
            g.hooked_work_id = None
        db.commit()
        print(f"cleared {len(bad_rows)} row hook(s) and {len(bad_groups)} card hook(s)")
        return 0
    finally:
        db.close()


# ------------------------------------------------------------------------------- list-broken
def cmd_list_broken(args) -> int:
    """Show works the integrity scan has flagged, so a maintenance session starts from evidence.

    Reads what the scanner already recorded; probes nothing."""
    db = SessionLocal()
    try:
        rows = db.scalars(
            select(Work).where(Work.health.in_(("missing", "corrupt", "mismatch")))
            .order_by(Work.media_kind, Work.id)).all()
        # A work whose health is 'ok' can still be PARTIALLY damaged — that is deliberate, because
        # 'corrupt' triggers a whole-title re-download and losing 19 good tracks to repair 1 is the
        # wrong trade. But health_detail is shared with benign status ("All discovered chapters
        # fetched."), so match the damage wording rather than merely "has a detail": doing the
        # latter reported 842 perfectly healthy works as damaged.
        detailed = [
            w for w in db.scalars(
                select(Work).where(Work.health == "ok", Work.health_detail.is_not(None))
                .order_by(Work.id)).all()
            if "unplayable" in (w.health_detail or "")
        ]
        if not rows and not detailed:
            print("nothing flagged.")
            return 0
        for w in rows:
            print(f"{w.health:9} {w.id:>7}  {(w.title or '')[:52]:54} {w.health_detail or ''}")
        for w in detailed:
            print(f"{'partial':9} {w.id:>7}  {(w.title or '')[:52]:54} {w.health_detail or ''}")
        print(f"\n{len(rows)} flagged, {len(detailed)} partially damaged.")
        return 0
    finally:
        db.close()


def main() -> None:
    ap = argparse.ArgumentParser(
        prog="shelfmanage",
        description="Library maintenance: adopt, repoint or remove a title. "
                    "Every command is a dry run unless --yes is given.")
    ap.add_argument("--yes", action="store_true", help="actually apply the change")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("adopt-audiobook", help="create a Work for an audiobook folder already on disk")
    a.add_argument("folder")
    a.add_argument("--title", help="override the title (default: embedded album tag, else folder name)")
    a.add_argument("--author", help="override the author (default: embedded album_artist tag)")
    a.add_argument("--series")
    # Float: novellas sit at fractional positions (2.5), same as the model.
    a.add_argument("--series-position", type=float)
    a.set_defaults(func=cmd_adopt_audiobook)

    r = sub.add_parser("repoint-work", help="point a Work at a different path on disk")
    r.add_argument("work_id", type=int)
    r.add_argument("path")
    r.set_defaults(func=cmd_repoint_work)

    d = sub.add_parser("remove-work", help="delete a Work and every back-pointer to it")
    d.add_argument("work_id", type=int)
    d.add_argument("--delete-files", action="store_true",
                   help="also remove its file/folder (refused when another Work shares the path)")
    d.set_defaults(func=cmd_remove_work)

    h = sub.add_parser("heal-hooks", help="clear catalog hooks pointing at an unrelated work")
    h.add_argument("--show", type=int, default=12, help="how many examples to list (default 12)")
    h.set_defaults(func=cmd_heal_hooks)

    b = sub.add_parser("list-broken", help="works the integrity scan has flagged")
    b.set_defaults(func=cmd_list_broken)

    args = ap.parse_args()
    sys.exit(args.func(args) or 0)


if __name__ == "__main__":
    main()
