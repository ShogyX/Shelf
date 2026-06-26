"""Per-user external reading-list imports (AniList, Goodreads, Open Library, Hardcover, MAL, Amazon
wishlist). A user previews a list, curates which titles to keep + corrects matches + picks the media
variant, then subscribes; ``list_sync_tick`` monitors it and auto-fetches newly-added titles. The poll
cadence is a global admin setting (Settings → list_sync_interval_hours)."""
from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import current_user
from ..db import get_db
from ..ingestion import list_import
from ..ingestion.extract import norm_title
from ..models import Bookshelf, ListSubscription, User
from ..schemas import (
    ListConfirmIn, ListPreviewIn, ListPreviewItemOut, ListPreviewOut, ListResolveIn, ListSubOut,
    ListSubUpdate,
)

router = APIRouter()

_VARIANTS = ("ebook", "audiobook", "both")


def _out(sub: ListSubscription) -> ListSubOut:
    return ListSubOut(
        id=sub.id, provider=sub.provider, list_ref=sub.list_ref, list_name=sub.list_name,
        display_name=sub.display_name, variant=sub.variant, target_shelf_id=sub.target_shelf_id,
        to_stock=sub.to_stock,
        active=sub.active, auto_series=sub.auto_series, auto_follow_series=sub.auto_follow_series,
        auto_added=sub.auto_added or 0, last_checked_at=sub.last_checked_at,
        last_error=sub.last_error, created_at=sub.created_at,
    )


def _validate_shelf(db: Session, user_id: int, shelf_id: int | None) -> int | None:
    if shelf_id is None:
        return None
    ok = db.scalar(select(Bookshelf.id).where(Bookshelf.id == shelf_id, Bookshelf.user_id == user_id))
    if ok is None:
        raise HTTPException(400, "That bookshelf doesn't exist or isn't yours")
    return shelf_id


def _validate_stock(db: Session, user: User, to_stock: bool) -> None:
    """A stock-destination import pre-fetches into the SHARED operator pool, so it's admin-only and
    needs the stock pipeline configured."""
    if not to_stock:
        return
    if user.role != "admin":
        raise HTTPException(403, "Only an admin can send a list to operator stock.")
    from ..ingestion import stock as stock_mod
    if not stock_mod.stock_configured(db):
        raise HTTPException(409, "Stocking needs the Prowlarr+SABnzbd pipeline and a stock directory "
                                 "(Settings → Integrations and the Stock page).")


@router.get("/list-imports/providers")
def providers() -> dict:
    """The supported providers + their selectable sub-lists, for the add-list UI."""
    labels = {"anilist": "AniList", "goodreads": "Goodreads", "openlibrary": "Open Library",
              "hardcover": "Hardcover", "mal": "MyAnimeList", "amazon_wishlist": "Amazon wishlist (Kindle)"}
    return {"providers": [
        {"key": p, "label": labels.get(p, p), "lists": list_import.PROVIDER_LISTS.get(p, [])}
        for p in list_import.PROVIDERS
    ]}


@router.post("/list-imports/preview", response_model=ListPreviewOut)
async def preview(payload: ListPreviewIn, user: User = Depends(current_user),
                  db: Session = Depends(get_db)) -> ListPreviewOut:
    """Read the external list and pair each title with a quick LOCAL catalog match (no live search) so
    the user can curate + correct before subscribing."""
    if payload.provider not in list_import.PROVIDERS:
        raise HTTPException(400, f"Unknown provider {payload.provider!r}")
    from ..ingestion.series import _pick_by_author
    try:
        items = await list_import.fetch_list(payload.provider, payload.list_ref,
                                             list_name=payload.list_name,
                                             config=list_import.provider_config(db))
    except list_import.ListImportError as exc:
        raise HTTPException(400, str(exc)) from exc
    out: list[ListPreviewItemOut] = []
    for it in items:
        row = _pick_by_author(db, norm_title(it.title), it.author, want_kind=it.media_kind)
        out.append(ListPreviewItemOut(
            title=it.title, author=it.author, media_kind=it.media_kind, cover_url=it.cover_url,
            match_catalog_id=row.id if row else None,
            match_title=row.title if row else None,
            match_author=row.author if row else None,
        ))
    return ListPreviewOut(provider=payload.provider, list_ref=payload.list_ref,
                          list_name=payload.list_name, count=len(out), items=out)


@router.post("/list-imports/resolve", response_model=list[ListPreviewItemOut])
async def resolve(payload: ListResolveIn, user: User = Depends(current_user),
                  db: Session = Depends(get_db)) -> list[ListPreviewItemOut]:
    """Resolve a chunk of previewed titles catalog-FIRST then UPSTREAM (book_catalog.resolve_live via
    series._resolve_book_row) — populating the catalog with metadata so the fetch pipeline has correct
    data. The frontend calls this in chunks (showing progress, blocking 'Add') until all are resolved."""
    from ..ingestion.series import _resolve_book_row
    if len(payload.items) > 30:
        raise HTTPException(400, "resolve at most 30 titles per request")
    out: list[ListPreviewItemOut] = []
    for it in payload.items:
        row = await _resolve_book_row(db, it.title, it.author, media_kind=it.media_kind)
        out.append(ListPreviewItemOut(
            title=it.title, author=it.author,
            match_catalog_id=row.id if row else None,
            match_title=row.title if row else None,
            match_author=row.author if row else None,
        ))
    return out


@router.get("/list-imports/{sub_id}/items", response_model=ListPreviewOut)
async def items(sub_id: int, user: User = Depends(current_user),
                db: Session = Depends(get_db)) -> ListPreviewOut:
    """An added list's current titles + covers (for the cover-row display). Served from the cached
    snapshot (no re-fetch); only falls back to a live fetch — and populates the cache — when the cache
    is empty (e.g. a list added before caching existed). Best-effort."""
    sub = _mine(db, sub_id, user.id)
    fetched = list_import.cached_items(db, sub_id)
    if not fetched:
        try:
            fetched = await list_import.fetch_list(sub.provider, sub.list_ref, list_name=sub.list_name,
                                                   config=list_import.provider_config(db))
        except list_import.ListImportError as exc:
            raise HTTPException(400, str(exc)) from exc
        list_import.cache_list_items(db, sub, fetched)
        db.commit()
    out = [ListPreviewItemOut(title=it.title, author=it.author, media_kind=it.media_kind,
                              cover_url=it.cover_url) for it in fetched]
    return ListPreviewOut(provider=sub.provider, list_ref=sub.list_ref, list_name=sub.list_name,
                          count=len(out), items=out)


async def _initial_sync(sub_id: int) -> None:
    """Background: run the first poll right after subscribing so the SELECTED titles start fetching
    immediately (capped per run; the rest drain via the hourly tick)."""
    from ..db import SessionLocal
    db = SessionLocal()
    try:
        sub = db.get(ListSubscription, sub_id)
        if sub is None:
            return
        try:
            await list_import.sync_list(db, sub)
        except list_import.ListImportError as exc:
            from ..models import _utcnow
            sub.last_error = str(exc)[:500]
            sub.last_checked_at = _utcnow()
            db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()
    finally:
        db.close()


@router.post("/list-imports", response_model=ListSubOut)
def create(payload: ListConfirmIn, bg: BackgroundTasks, user: User = Depends(current_user),
           db: Session = Depends(get_db)) -> ListSubOut:
    """Subscribe to a list. Titles the user UNSELECTED are baselined (never fetched); SELECTED titles
    become 'new' and are fetched by the immediate background poll + the monitor tick. Settings (variant,
    target shelf, list_name) are saved and editable later."""
    if payload.provider not in list_import.PROVIDERS:
        raise HTTPException(400, f"Unknown provider {payload.provider!r}")
    if payload.variant not in _VARIANTS:
        raise HTTPException(400, f"variant must be one of {_VARIANTS}")
    _validate_shelf(db, user.id, payload.target_shelf_id)
    _validate_stock(db, user, payload.to_stock)
    if db.scalar(select(ListSubscription.id).where(
            ListSubscription.user_id == user.id, ListSubscription.provider == payload.provider,
            ListSubscription.list_ref == payload.list_ref)):
        raise HTTPException(409, "You've already added this list")
    # Baseline = the UNSELECTED titles, so only the selected ones (and future additions) are fetched.
    baseline = sorted({norm_title(i.title) for i in payload.items if not i.selected and i.title})
    sub = ListSubscription(
        user_id=user.id, provider=payload.provider, list_ref=payload.list_ref,
        list_name=payload.list_name, display_name=payload.display_name, variant=payload.variant,
        target_shelf_id=payload.target_shelf_id, to_stock=payload.to_stock, active=True,
        known_keys=baseline, last_checked_at=None,
        auto_series=payload.auto_series, auto_follow_series=payload.auto_follow_series,
    )
    db.add(sub)
    db.commit()
    db.refresh(sub)
    # Seed the item cache from the previewed payload so GET /items serves instantly (the background
    # initial-sync then enriches covers/refs on its first lightweight fetch). title+author only —
    # the confirm payload carries no covers, and the scan never resolves them.
    list_import.cache_list_items(db, sub, [
        list_import.ListItem(title=i.title, author=i.author) for i in payload.items if i.title])
    db.commit()
    bg.add_task(_initial_sync, sub.id)
    return _out(sub)


@router.get("/list-imports", response_model=list[ListSubOut])
def list_mine(user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[ListSubOut]:
    subs = db.scalars(select(ListSubscription).where(ListSubscription.user_id == user.id)
                      .order_by(ListSubscription.id.desc())).all()
    return [_out(s) for s in subs]


def _mine(db: Session, sub_id: int, user_id: int) -> ListSubscription:
    sub = db.get(ListSubscription, sub_id)
    if sub is None or sub.user_id != user_id:
        raise HTTPException(404, "List import not found")
    return sub


@router.patch("/list-imports/{sub_id}", response_model=ListSubOut)
def update(sub_id: int, payload: ListSubUpdate, user: User = Depends(current_user),
           db: Session = Depends(get_db)) -> ListSubOut:
    sub = _mine(db, sub_id, user.id)
    if payload.variant is not None:
        if payload.variant not in _VARIANTS:
            raise HTTPException(400, f"variant must be one of {_VARIANTS}")
        sub.variant = payload.variant
    if "target_shelf_id" in payload.model_fields_set:
        sub.target_shelf_id = _validate_shelf(db, user.id, payload.target_shelf_id)
    if payload.to_stock is not None:
        _validate_stock(db, user, payload.to_stock)
        sub.to_stock = payload.to_stock
    if payload.active is not None:
        sub.active = payload.active
    if payload.auto_series is not None:
        sub.auto_series = payload.auto_series
    if payload.auto_follow_series is not None:
        sub.auto_follow_series = payload.auto_follow_series
    if payload.list_name is not None:
        sub.list_name = payload.list_name
    if payload.list_ref is not None and payload.list_ref.strip():
        sub.list_ref = payload.list_ref.strip()
    if payload.display_name is not None and payload.display_name.strip():
        sub.display_name = payload.display_name.strip()
    db.commit()
    db.refresh(sub)
    return _out(sub)


@router.delete("/list-imports/{sub_id}")
def delete(sub_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    from sqlalchemy import delete as sa_delete
    from ..models import ListSubscriptionItem
    sub = _mine(db, sub_id, user.id)
    # SQLite FK cascade isn't enforced (no PRAGMA foreign_keys=ON), so clear the cached items here.
    db.execute(sa_delete(ListSubscriptionItem).where(ListSubscriptionItem.subscription_id == sub_id))
    db.delete(sub)
    db.commit()
    return {"deleted": True}


@router.post("/list-imports/{sub_id}/sync", response_model=ListSubOut)
async def sync_now(sub_id: int, user: User = Depends(current_user),
                   db: Session = Depends(get_db)) -> ListSubOut:
    """Manual 'check now': re-poll this list immediately and fetch any new titles (capped per run)."""
    sub = _mine(db, sub_id, user.id)
    try:
        await list_import.sync_list(db, sub)
    except list_import.ListImportError as exc:
        from ..models import _utcnow
        sub.last_error = str(exc)[:500]
        sub.last_checked_at = _utcnow()
        db.commit()
        raise HTTPException(400, str(exc)) from exc
    db.refresh(sub)
    return _out(sub)
