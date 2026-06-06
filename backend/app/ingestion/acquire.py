"""Acquisition routing — pick HOW to obtain a catalog work.

A logical work can be obtainable several ways: crawled from a web-index source (hook), pulled by a
connected library manager (Readarr/Kapowarr grab), or downloaded via the usenet pipeline
(Prowlarr→SABnzbd). The operator sets a default priority order; each user may override it; and a
user may pick a specific route per title. Manual acquisition and auto-fetch (Goodreads / catalog)
both resolve a title down the same priority list.
"""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import AppSetting, CatalogWork, Integration

log = logging.getLogger("shelf.acquire")

ROUTES = ("pipeline", "web_index", "readarr", "kapowarr")
DEFAULT_PRIORITY = ["pipeline", "web_index", "readarr", "kapowarr"]
_GLOBAL_KEY = "fetch_source_priority"


def _clean(order) -> list[str]:
    seen, out = set(), []
    for r in order or []:
        if r in ROUTES and r not in seen:
            seen.add(r)
            out.append(r)
    # Append any routes the caller omitted so resolution always has a full fallback chain.
    for r in DEFAULT_PRIORITY:
        if r not in seen:
            out.append(r)
    return out


def global_priority(db: Session) -> list[str]:
    row = db.get(AppSetting, _GLOBAL_KEY)
    return _clean(row.value if row and isinstance(row.value, list) else None)


def set_global_priority(db: Session, order: list[str]) -> list[str]:
    val = _clean(order)
    row = db.get(AppSetting, _GLOBAL_KEY)
    if row is None:
        db.add(AppSetting(key=_GLOBAL_KEY, value=val))
    else:
        row.value = val
    db.commit()
    return val


def _user_key(user_id: int) -> str:
    return f"{_GLOBAL_KEY}:user:{user_id}"


def user_priority(db: Session, user) -> list[str]:
    """A user's effective route priority: their override, else the global default."""
    if user is not None:
        row = db.get(AppSetting, _user_key(user.id))
        if row and isinstance(row.value, list):
            return _clean(row.value)
    return global_priority(db)


def set_user_priority(db: Session, user_id: int, order: list[str] | None) -> list[str]:
    """Set (or clear, with None) a user's override. Returns the effective list."""
    key = _user_key(user_id)
    row = db.get(AppSetting, key)
    if order is None:
        if row is not None:
            db.delete(row)
        db.commit()
        return global_priority(db)
    val = _clean(order)
    if row is None:
        db.add(AppSetting(key=key, value=val))
    else:
        row.value = val
    db.commit()
    return val


def _members(db: Session, rep: CatalogWork) -> list[CatalogWork]:
    """The catalog rows clustered with `rep` (same normalized title + media class)."""
    if not rep.norm_key:  # empty key would match every untitled row — just use this one
        return [rep]
    bucket = "comic" if (rep.media_kind or "text") == "comic" else "text"
    rows = db.scalars(
        select(CatalogWork).where(CatalogWork.norm_key == rep.norm_key)
    ).all()
    same = [r for r in rows if ("comic" if (r.media_kind or "text") == "comic" else "text") == bucket]
    return same or [rep]


def available_routes(db: Session, rep: CatalogWork) -> list[str]:
    """Which routes can actually fulfill this work right now (for the UI's route picker)."""
    members = _members(db, rep)
    out: list[str] = []
    if any(m.provider == "web_index" and m.hooked_work_id is None for m in members):
        out.append("web_index")
    for kind in ("readarr", "kapowarr"):
        if any(m.provider == kind and m.integration_id for m in members):
            out.append(kind)
    sab = db.scalar(select(Integration).where(Integration.kind == "sabnzbd", Integration.enabled.is_(True)))
    prow = db.scalar(select(Integration).where(Integration.kind == "prowlarr", Integration.enabled.is_(True)))
    if sab is not None and prow is not None:
        out.append("pipeline")
    return out


async def acquire(
    db: Session, rep: CatalogWork, *, user_id: int | None, priority: list[str],
    shelf_id: int | None = None, route: str | None = None,
) -> dict:
    """Acquire `rep`'s work via the first route (in `priority`, or just `route` if forced) that can
    fulfill it. Returns {"route", "status", ...}. ``status``: hooked | grabbed | downloading | none."""
    from . import catalog, downloads
    from ..integrations import sync as isync
    from ..library import add_to_library

    if rep.hooked_work_id is not None:
        if user_id:
            add_to_library(db, user_id, rep.hooked_work_id, shelf_id=shelf_id)
        return {"route": "library", "status": "hooked", "work_id": rep.hooked_work_id}

    members = _members(db, rep)
    order = [route] if route else priority
    last_err: str | None = None
    for r in order:
        if r == "web_index":
            cand = next((m for m in members if m.provider == "web_index" and m.hooked_work_id is None), None)
            if cand is None:
                continue
            try:
                work = await catalog.hook_entry(db, cand)
            except Exception as exc:  # noqa: BLE001 — try the next route
                last_err = f"web_index: {exc}"
                continue
            if user_id:
                add_to_library(db, user_id, work.id, shelf_id=shelf_id)
            return {"route": "web_index", "status": "hooked", "work_id": work.id}

        if r in ("readarr", "kapowarr"):
            cand = next((m for m in members if m.provider == r and m.integration_id), None)
            if cand is None:
                continue
            try:
                await isync.grab_external(db, cand)
                return {"route": r, "status": "grabbed", "catalog_id": cand.id}
            except Exception as exc:  # noqa: BLE001
                last_err = f"{r}: {exc}"
                continue

        if r == "pipeline":
            if "pipeline" not in available_routes(db, rep):
                continue
            # Prefer a real book row (richer title/author) to drive the Prowlarr match.
            cw = max(members, key=lambda m: (m.provider in ("googlebooks", "openlibrary"), bool(m.author)))
            try:
                job = await downloads.auto_grab(db, cw, user_id=user_id, shelf_id=shelf_id)
            except Exception as exc:  # noqa: BLE001
                last_err = f"pipeline: {exc}"
                continue
            if job is not None:
                return {"route": "pipeline", "status": "downloading", "job_id": job.id}
            last_err = "pipeline: no confident release match"

    return {"route": None, "status": "none", "detail": last_err}
