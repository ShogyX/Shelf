"""Per-user library membership.

The library is PER-USER: a ``LibraryItem`` is a user's membership of a (global, shared) ``Work``.
Works + their crawl/chapters are shared across users — one crawl serves everyone — so hooking a
title that's already hooked just adds a membership and never re-crawls. Bookshelves organize a
user's library; placing a work on a shelf implies membership.
"""
from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from .models import (
    Bookshelf,
    BookshelfItem,
    Chapter,
    CatalogGroup,
    CatalogWork,
    CrawlJob,
    IndexedPage,
    LibraryItem,
    MetadataLink,
    QueuedHook,
    User,
    Work,
)


def assert_work_access(db: Session, user: User, work_id: int) -> None:
    """Library isolation gate: raise 404 unless ``work_id`` is readable by ``user``. Readable =
    admin, OR it's in their library, OR it's an in-stock (hooked) catalog title they may preview —
    one the catalog would show them (``readable_in_stock``). 404 (not 403) so a non-member can't
    probe which works exist. Use on every endpoint that returns or acts on a work's content
    (chapters, progress, delivery)."""
    if user.role == "admin":
        return
    if in_library(db, user.id, work_id):
        return
    if readable_in_stock(db, user, work_id):
        return
    raise HTTPException(404, "Work not found")


def readable_in_stock(db: Session, user: User, work_id: int) -> bool:
    """True when ``work_id`` is an in-stock (hooked) title this user may PREVIEW-read without first
    adding it to their library: some ``CatalogGroup`` hooked to it is visible to them under the
    catalog's own category-cap + 18+ gating. This is what makes the "Open to read" affordance on an
    in-stock title actually open (previously a non-member got a 404). Gating on catalog visibility
    means preview can never reveal content the catalog itself hides from this viewer."""
    from .ingestion.catalog import group_visible

    groups = db.scalars(
        select(CatalogGroup).where(CatalogGroup.hooked_work_id == work_id)
    ).all()
    return any(group_visible(db, user, g) for g in groups)


def validate_shelf(db: Session, user_id: int, shelf_id: int | None) -> int | None:
    """Confirm ``shelf_id`` (if given) is a bookshelf owned by ``user_id``; raise 400 otherwise.
    Use on acquire/grab paths that record the shelf for *deferred* placement (download jobs), where
    ``add_to_library``'s silent ownership check would otherwise drop a bad shelf without a signal."""
    if shelf_id is None:
        return None
    owns = db.scalar(
        select(Bookshelf.id).where(Bookshelf.id == shelf_id, Bookshelf.user_id == user_id)
    )
    if not owns:
        raise HTTPException(400, "That bookshelf doesn't exist or isn't yours.")
    return shelf_id


def add_to_library(db: Session, user_id: int, work_id: int, *, shelf_id: int | None = None) -> bool:
    """Ensure ``work_id`` is in ``user_id``'s library (idempotent). Returns True if newly added.
    With ``shelf_id``, also place it on that bookshelf (the shelf must belong to the user)."""
    added = False
    exists = db.scalar(
        select(LibraryItem.id).where(
            LibraryItem.user_id == user_id, LibraryItem.work_id == work_id
        )
    )
    if not exists:
        db.add(LibraryItem(user_id=user_id, work_id=work_id))
        added = True
    if shelf_id is not None:
        owns = db.scalar(
            select(Bookshelf.id).where(Bookshelf.id == shelf_id, Bookshelf.user_id == user_id)
        )
        on_shelf = db.scalar(
            select(BookshelfItem.id).where(
                BookshelfItem.shelf_id == shelf_id, BookshelfItem.work_id == work_id
            )
        )
        if owns and not on_shelf:
            db.add(BookshelfItem(shelf_id=shelf_id, work_id=work_id))
    db.commit()
    return added


def ensure_named_shelf(db: Session, user_id: int, name: str, **flags) -> Bookshelf:
    """Get-or-create the user's bookshelf named ``name``, applying ``flags`` (e.g. auto_kindle=True,
    goodreads_target=True) when it's newly created. Idempotent: an existing shelf is returned
    untouched so a user's later edits aren't clobbered. Used to auto-provision the default Kindle /
    Goodreads shelves."""
    shelf = db.scalar(
        select(Bookshelf).where(Bookshelf.user_id == user_id, Bookshelf.name == name)
    )
    if shelf is not None:
        return shelf
    nxt = (db.scalar(select(func.max(Bookshelf.sort_order)).where(
        Bookshelf.user_id == user_id)) or 0) + 1
    shelf = Bookshelf(user_id=user_id, name=name, sort_order=nxt, **flags)
    db.add(shelf)
    db.commit()
    db.refresh(shelf)
    return shelf


def in_library(db: Session, user_id: int, work_id: int) -> bool:
    return db.scalar(
        select(LibraryItem.id).where(
            LibraryItem.user_id == user_id, LibraryItem.work_id == work_id
        )
    ) is not None


def library_work_ids(db: Session, user_id: int) -> set[int]:
    return set(db.scalars(select(LibraryItem.work_id).where(LibraryItem.user_id == user_id)).all())


def remove_from_library(db: Session, user_id: int, work_id: int) -> None:
    """Drop the user's membership + remove the work from any of THEIR bookshelves. The shared Work
    (and its chapters/crawl) is left intact — other users may still have it. Global deletion of the
    Work is a separate, explicit admin action."""
    db.execute(
        delete(LibraryItem).where(
            LibraryItem.user_id == user_id, LibraryItem.work_id == work_id
        )
    )
    shelf_ids = select(Bookshelf.id).where(Bookshelf.user_id == user_id)
    db.execute(
        delete(BookshelfItem).where(
            BookshelfItem.work_id == work_id, BookshelfItem.shelf_id.in_(shelf_ids)
        )
    )
    db.commit()


def purge_work(db: Session, work: Work, *, delete_files: bool = False) -> None:
    """Permanently destroy a shared Work and every trace of it: memberships, shelf placements,
    crawl jobs, metadata links, and the catalog/index/queue "hooked" pointers that reference it
    (SQLite FK enforcement is off, so nothing nulls/cascades these automatically). The Work's own
    chapters, content and reading-states cascade via the ORM relationships on ``db.delete(work)``.

    ``delete_files``: also remove the work's on-disk file/folder (admin "remove the item and file"),
    guarded so a path another Work still uses (e.g. a shared audiobook folder) is never touched.

    Lives here (not in a router) so ingestion paths that remove a Work — e.g. the watched-folder
    sync when a file vanishes — clean up back-pointers too, instead of leaving dangling refs."""
    work_id = work.id
    local = (work.local_path or "").strip() if delete_files else None
    db.execute(delete(LibraryItem).where(LibraryItem.work_id == work_id))
    db.execute(delete(BookshelfItem).where(BookshelfItem.work_id == work_id))
    db.execute(delete(MetadataLink).where(MetadataLink.work_id == work_id))
    for job in db.scalars(select(CrawlJob).where(CrawlJob.work_id == work_id)).all():
        db.delete(job)
    # Clear every "hooked at this work" back-pointer so none dangle at a deleted work.
    db.execute(update(CatalogWork).where(CatalogWork.hooked_work_id == work_id)
               .values(hooked_work_id=None, health="unknown", health_detail=None))
    db.execute(update(CatalogGroup).where(CatalogGroup.hooked_work_id == work_id)
               .values(hooked_work_id=None))
    db.execute(update(IndexedPage).where(IndexedPage.hooked_work_id == work_id)
               .values(hooked_work_id=None))
    db.execute(update(QueuedHook).where(QueuedHook.related_work_id == work_id)
               .values(related_work_id=None))
    db.execute(update(QueuedHook).where(QueuedHook.hooked_work_id == work_id)
               .values(hooked_work_id=None))
    db.delete(work)
    db.commit()
    if local:
        # Never delete a path another live Work still points at (shared audiobook folders).
        alive = {p for (p,) in db.execute(
            select(Work.local_path).where(Work.local_path.is_not(None))).all() if p}
        from .ingestion.dedup import _safe_delete
        _safe_delete(local, alive)


def _readable_work_ids(db: Session, work_ids: set[int]) -> set[int]:
    """Subset of ``work_ids`` that are actually readable: they have at least one chapter with stored
    content. (Imported ebooks are chapterized on import; web works get content as they're crawled —
    so a text/comic Work with zero content-bearing chapters has nothing to show.)"""
    if not work_ids:
        return set()
    return set(db.scalars(
        select(Chapter.work_id)
        .where(Chapter.work_id.in_(work_ids), Chapter.content_id.is_not(None))
        .distinct()
    ).all())


def _crawling_work_ids(db: Session, work_ids: set[int]) -> set[int]:
    """Subset of ``work_ids`` with an in-flight crawl (still gathering) — so a freshly-hooked work
    that hasn't fetched content YET isn't mistaken for dead stock."""
    if not work_ids:
        return set()
    return set(db.scalars(
        select(CrawlJob.work_id)
        .where(CrawlJob.work_id.in_(work_ids),
               CrawlJob.status.in_(("scheduled", "running", "paused")))
        .distinct()
    ).all())


def unhook_dead_stock(db: Session) -> dict:
    """Clear "hooked to a shared Work" back-pointers that point at DEAD stock, so those catalog
    entries stop reporting ``in_stock`` and revert to "Acquire".

    Dead = the target Work no longer exists, OR it's a text/comic Work with no readable content and
    no active crawl (a failed/empty import — e.g. a book whose file never materialized — that would
    otherwise show "In stock — open to read" but open blank). Audiobooks are referenced via
    ``audiobook_work_id`` (never ``hooked_work_id`` — the hooking invariant keeps audio Works off the
    hook pointers), so they're never mis-swept here. Returns per-pointer counts of what was cleared."""
    zero = {"catalog_groups": 0, "catalog_works": 0, "indexed_pages": 0, "queued_hooks": 0}
    referenced: set[int] = set()
    for col in (CatalogGroup.hooked_work_id, CatalogWork.hooked_work_id,
                IndexedPage.hooked_work_id, QueuedHook.hooked_work_id):
        referenced |= set(db.scalars(select(col).where(col.is_not(None)).distinct()).all())
    if not referenced:
        return zero
    existing = set(db.scalars(select(Work.id).where(Work.id.in_(referenced))).all())
    readable = _readable_work_ids(db, existing)
    crawling = _crawling_work_ids(db, existing - readable)
    dead = {wid for wid in referenced
            if wid not in existing or (wid not in readable and wid not in crawling)}
    if not dead:
        return zero
    counts = {
        "catalog_groups": db.execute(
            update(CatalogGroup).where(CatalogGroup.hooked_work_id.in_(dead))
            .values(hooked_work_id=None)).rowcount,
        "catalog_works": db.execute(
            update(CatalogWork).where(CatalogWork.hooked_work_id.in_(dead))
            .values(hooked_work_id=None, health="unknown", health_detail=None)).rowcount,
        "indexed_pages": db.execute(
            update(IndexedPage).where(IndexedPage.hooked_work_id.in_(dead))
            .values(hooked_work_id=None)).rowcount,
        "queued_hooks": db.execute(
            update(QueuedHook).where(QueuedHook.hooked_work_id.in_(dead))
            .values(hooked_work_id=None)).rowcount,
    }
    db.commit()
    return counts
