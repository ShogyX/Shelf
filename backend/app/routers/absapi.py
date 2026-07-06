"""Audiobookshelf-compatible API surface.

Lets an Audiobookshelf companion app (e.g. "Still") connect to Shelf natively: log in, browse the
libraries, open + stream an item, sync listening progress, and manage collections. Shelf concepts map
onto ABS ones:

    ABS library "Audiobooks"  <- stocked audio Works        (media_kind="audio")
    ABS library "Books"       <- stocked ebook Works         (media_kind="text")
    ABS library "Comics"      <- stocked comic Works         (media_kind="comic")
    ABS libraryItem           <- a Work                      (id = str(work.id))
    ABS mediaProgress         <- ReadingState audio position (per user+work)
    ABS collection            <- a user Bookshelf            (id = "col_<shelf.id>")

Auth is Shelf's ordinary session token presented as an ABS bearer token (issued by POST /login);
auth.request_session_token also accepts it as ?token= on the media URLs an ABS client builds. Actions
taken in the app (progress, collection edits) write straight to Shelf's own tables, so they show up in
the Shelf web UI too. Podcasts, server-admin and multi-user management are intentionally out of scope.
"""
from __future__ import annotations

import base64
import mimetypes
import os
import time
from urllib.parse import quote, unquote

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..auth import create_session, current_user, request_session_token
from ..db import get_db
from ..models import Bookshelf, BookshelfItem, ReadingState, User, Work, _utcnow
from .delivery import _global_pos, _probe_audio

router = APIRouter()

# id -> (display name, media_kinds, is_audio). Each library is the whole STOCKED pool of its kind
# (Work.local_path set) — the same shared model audiobooks already used, so a user reaches every
# downloaded title (their library items are a subset of stock).
_LIBS: dict[str, tuple[str, tuple[str, ...], bool]] = {
    "shelf-audiobooks": ("Audiobooks", ("audio",), True),
    "shelf-books": ("Books", ("text",), False),
    "shelf-comics": ("Comics", ("comic",), False),
}
_DEFAULT_LIB = "shelf-audiobooks"
_FINISHED_AT = 0.985   # progress fraction at/above which a title reads as finished (ABS convention)


# --------------------------------------------------------------------------------- time + queries
def _ms(dt) -> int:
    if dt is None:
        return 0
    try:
        return int(dt.timestamp() * 1000)
    except (OSError, OverflowError, ValueError):
        return 0


def _now_ms() -> int:
    return int(time.time() * 1000)


def _lib(library_id: str) -> tuple[str, tuple[str, ...], bool]:
    cfg = _LIBS.get(library_id)
    if cfg is None:
        raise HTTPException(404, "Library not found")
    return cfg


def _kinds_query(kinds: tuple[str, ...]):
    return select(Work).where(Work.media_kind.in_(kinds), Work.local_path.is_not(None))


def _library_page(db: Session, library_id: str, limit: int, offset: int,
                  extra: tuple = ()) -> tuple[list[Work], int]:
    _name, kinds, _audio = _lib(library_id)
    conds = (Work.media_kind.in_(kinds), Work.local_path.is_not(None), *extra)
    total = db.scalar(select(func.count()).select_from(Work).where(*conds)) or 0
    q = select(Work).where(*conds).order_by(Work.title)
    if limit:
        q = q.limit(limit).offset(offset)
    return list(db.scalars(q).all()), total


def _filter_conds(filter_str: str) -> tuple:
    """Translate an ABS list filter (`authors.<base64>` / `narrators.…` / `series.…`) into a WHERE
    clause, so tapping an author/narrator/series shows THAT one's books, not the whole library. The
    base64 value is our own id (aut_/nrt_/ser_ + url-quoted name), so decode it back to the name."""
    if not filter_str or "." not in filter_str:
        return ()
    group, _, b64 = filter_str.partition(".")
    try:
        val = base64.b64decode(b64 + "===").decode("utf-8", "ignore")
    except Exception:  # noqa: BLE001 — a malformed filter just means "no filter"
        return ()
    for pre in ("aut_", "nrt_", "ser_"):
        if val.startswith(pre):
            val = unquote(val[len(pre):])
            break
    if group == "authors":
        return (Work.author == val,)
    if group == "narrators":
        return (Work.narrator == val,)
    if group == "series":
        return (Work.series == val,)
    return ()


def _audio_works(db: Session) -> list[Work]:
    return list(db.scalars(_kinds_query(("audio",)).order_by(Work.title)).all())


def _get_item(db: Session, item_id: str) -> Work:
    try:
        wid = int(item_id)
    except (TypeError, ValueError):
        raise HTTPException(404, "Item not found")
    work = db.get(Work, wid)
    if work is None or not work.local_path:
        raise HTTPException(404, "Item not found")
    return work


def _duration_s(work: Work) -> float:
    meta = work.audio_meta if isinstance(work.audio_meta, dict) else None
    if meta and isinstance(meta.get("total_duration_s"), (int, float)):
        return float(meta["total_duration_s"])
    return 0.0


_EXT_MIME = {".epub": "application/epub+zip", ".mobi": "application/x-mobipocket-ebook",
             ".azw3": "application/vnd.amazon.ebook", ".azw": "application/vnd.amazon.ebook",
             ".pdf": "application/pdf", ".txt": "text/plain",
             ".cbz": "application/vnd.comicbook+zip", ".cbr": "application/vnd.comicbook-rar",
             ".m4b": "audio/mp4", ".m4a": "audio/mp4", ".mp3": "audio/mpeg", ".flac": "audio/flac"}


def _ebook_fmt(work: Work) -> str:
    """The ebook/comic file's format (epub | mobi | pdf | cbz | …) from its actual extension."""
    return (os.path.splitext(work.local_path or "")[1].lower().lstrip(".")) or "epub"


def _serve_work_file(work: Work, *, download: bool):
    """Serve a Work's on-disk file (the original epub/mobi/pdf/cbz, or a single-file audiobook). Powers
    the ABS ebook-reader + item/file download; FileResponse handles Range so large files stream. A
    multi-file (folder) audiobook has no single file — those are fetched track-by-track via the play
    session's contentUrls instead."""
    path = work.local_path or ""
    if not path or not os.path.isfile(path):
        raise HTTPException(404, "File not available")
    ext = os.path.splitext(path)[1].lower()
    mime = _EXT_MIME.get(ext) or mimetypes.guess_type(path)[0] or "application/octet-stream"
    name = os.path.basename(path)
    return FileResponse(path, media_type=mime, filename=(name if download else None))


# --------------------------------------------------------------------------------- ABS item shapes
def _metadata(work: Work, *, minified: bool) -> dict:
    author, narrator = work.author or "", work.narrator or ""
    md = {
        "title": work.title or "", "titleIgnorePrefix": work.title or "", "subtitle": None,
        "authorName": author, "narratorName": narrator, "seriesName": work.series or "",
        "genres": list(work.genres or []) if isinstance(work.genres, list) else [],
        "publishedYear": str(work.year) if work.year else None, "publisher": work.publisher or None,
        "description": work.description or "", "isbn": None, "asin": None,
        "language": work.language or None, "explicit": False,
    }
    if not minified:
        md["authors"] = [{"id": f"aut_{work.id}", "name": author}] if author else []
        md["narrators"] = [narrator] if narrator else []
        md["series"] = ([{"id": f"ser_{work.series_id or work.id}", "name": work.series,
                          "sequence": (str(work.series_position) if work.series_position else "")}]
                        if work.series else [])
    return md


def _abs_tracks(work_id: int, meta: dict | None, token: str | None) -> list[dict]:
    if not meta:
        return []
    q = f"?token={token}" if token else ""
    out, offset = [], 0.0
    for t in meta["tracks"]:
        dur = float(t["duration_s"])
        out.append({
            "index": t["index"], "startOffset": offset, "duration": dur,
            "title": t.get("title") or f"Track {t['index']}",
            "contentUrl": f"/api/works/{work_id}/audio/stream/{t['index']}{q}",
            "mimeType": "audio/mp4" if not t["native"] else "audio/mpeg",
            "metadata": {"filename": f"{t['index']}", "ext": t.get("ext") or "", "path": "", "size": 0},
        })
        offset += dur
    return out


def _abs_chapters(meta: dict | None) -> list[dict]:
    if not meta:
        return []
    out = []
    for i, c in enumerate(meta["chapters"]):
        start = float(c.get("global_start_s", 0.0))
        out.append({"id": i, "start": start, "end": start, "title": c.get("title") or f"Chapter {i + 1}"})
    total = float(meta.get("total_duration_s", 0.0))
    for i in range(len(out)):
        out[i]["end"] = out[i + 1]["start"] if i + 1 < len(out) else total
    return out


def _library_id_for(work: Work) -> str:
    return {"audio": "shelf-audiobooks", "comic": "shelf-comics"}.get(work.media_kind or "text", "shelf-books")


def _library_item(work: Work, *, minified: bool, db: Session | None = None, token: str | None = None) -> dict:
    is_audio = work.media_kind == "audio"
    dur = _duration_s(work) if is_audio else 0.0
    added = _ms(work.created_at)
    media: dict = {
        "libraryItemId": str(work.id), "metadata": _metadata(work, minified=minified),
        "coverPath": f"/api/items/{work.id}/cover" if work.cover_url else None,
        "tags": [], "duration": dur, "size": work.local_size or 0,
    }
    if minified:
        nch = len(work.audio_meta.get("chapters", [])) if (is_audio and isinstance(work.audio_meta, dict)) else 0
        media.update({"numTracks": 1 if is_audio else 0, "numAudioFiles": 1 if is_audio else 0,
                      "numChapters": nch, "numEbooks": 0 if is_audio else 1,
                      "ebookFormat": None if is_audio else _ebook_fmt(work)})
    elif is_audio:
        meta = _probe_audio(db, work) if db is not None else (work.audio_meta if isinstance(work.audio_meta, dict) else None)
        media.update({"audioFiles": [], "ebookFile": None,
                      "chapters": _abs_chapters(meta), "tracks": _abs_tracks(work.id, meta, token)})
        if meta and not dur:
            media["duration"] = float(meta.get("total_duration_s", 0.0))
    else:
        fmt = _ebook_fmt(work)
        fname = os.path.basename(work.local_path or "") or f"{work.id}.{fmt}"
        fmeta = {"filename": fname, "ext": f".{fmt}", "path": work.local_path or "",
                 "relPath": fname, "size": work.local_size or 0}
        # ebookFormat MUST be at the media level too (not only inside ebookFile) — the ereader reads
        # media.ebookFormat to decide the item is a readable ebook; a null there = "no book to open".
        media.update({"audioFiles": [], "tracks": [], "chapters": [], "ebookFormat": fmt,
                      "numTracks": 0, "numAudioFiles": 0, "numChapters": 0, "numEbooks": 1,
                      "ebookFile": {"ino": str(work.id), "ebookFormat": fmt, "isSupplementary": False,
                                    "addedAt": added, "updatedAt": added, "metadata": fmeta}})
    # A libraryFiles entry for the ebook so clients that locate the file that way also find it.
    library_files = ([] if (is_audio or minified) else
                     [{"ino": str(work.id), "fileType": "ebook", "addedAt": added, "updatedAt": added,
                       "metadata": {"filename": os.path.basename(work.local_path or "") or f"{work.id}",
                                    "ext": os.path.splitext(work.local_path or "")[1], "path": work.local_path or "",
                                    "size": work.local_size or 0}}])
    return {
        "id": str(work.id), "ino": str(work.id), "libraryId": _library_id_for(work), "folderId": _library_id_for(work),
        "path": work.local_path or "", "relPath": work.local_path or "", "isFile": True,
        "mtimeMs": 0, "ctimeMs": 0, "birthtimeMs": 0, "addedAt": added,
        "updatedAt": _ms(work.last_update_at) or added, "isMissing": False, "isInvalid": False,
        "mediaType": "book", "media": media, "libraryFiles": library_files, "numFiles": 1,
        "size": work.local_size or 0,
    }


# --------------------------------------------------------------------------------- progress
def _progress_dict(user_id: int, work: Work, st: ReadingState) -> dict:
    meta = work.audio_meta if isinstance(work.audio_meta, dict) else None
    cur, total = _global_pos(meta, st.audio_track or 0, st.audio_pos_s or 0.0)
    frac = (cur / total) if total else 0.0
    return {
        "id": f"{user_id}-{work.id}", "libraryItemId": str(work.id), "episodeId": None,
        "duration": total, "progress": min(frac, 1.0), "currentTime": cur,
        "isFinished": frac >= _FINISHED_AT, "hideFromContinueListening": False,
        "lastUpdate": _ms(st.audio_updated_at), "startedAt": _ms(st.audio_updated_at),
        "finishedAt": _ms(st.audio_updated_at) if frac >= _FINISHED_AT else None,
    }


def _media_progress(user_id: int, work: Work, db: Session) -> dict | None:
    st = db.scalar(select(ReadingState).where(
        ReadingState.user_id == user_id, ReadingState.work_id == work.id))
    if st is None or st.audio_updated_at is None:
        return None
    return _progress_dict(user_id, work, st)


def _all_progress(user_id: int, db: Session) -> list[dict]:
    """Every audiobook the user has progress on — ONE ReadingState query, not per-work (N+1)."""
    works = _audio_works(db)
    if not works:
        return []
    states = {s.work_id: s for s in db.scalars(select(ReadingState).where(
        ReadingState.user_id == user_id, ReadingState.work_id.in_([w.id for w in works]),
        ReadingState.audio_updated_at.is_not(None))).all()}
    return [_progress_dict(user_id, w, states[w.id]) for w in works if w.id in states]


def _global_to_track(meta: dict | None, current_s: float) -> tuple[int, float]:
    """Inverse of delivery._global_pos: a global position -> (track index, offset within that track)."""
    if not meta or not meta.get("tracks"):
        return 0, max(0.0, current_s)
    offset = 0.0
    for t in meta["tracks"]:
        dur = float(t["duration_s"])
        if current_s < offset + dur or t is meta["tracks"][-1]:
            return t["index"], max(0.0, current_s - offset)
        offset += dur
    return meta["tracks"][-1]["index"], 0.0


def _write_progress(db: Session, user_id: int, work: Work, current_s: float, *, finished: bool | None = None) -> None:
    """Persist a listening position back to Shelf's ReadingState so it shows in the web UI too."""
    meta = work.audio_meta if isinstance(work.audio_meta, dict) else _probe_audio(db, work)
    if finished and meta:
        current_s = float(meta.get("total_duration_s", 0.0)) or current_s
    track, pos = _global_to_track(meta, current_s)
    st = db.scalar(select(ReadingState).where(
        ReadingState.user_id == user_id, ReadingState.work_id == work.id))
    if st is None:
        st = ReadingState(user_id=user_id, work_id=work.id)
        db.add(st)
    st.audio_track, st.audio_pos_s, st.audio_updated_at = track, pos, _utcnow()
    db.commit()


# --------------------------------------------------------------------------------- collections
def _shelf_from_collection(db: Session, user: User, collection_id: str) -> Bookshelf:
    try:
        sid = int(collection_id.removeprefix("col_"))
    except (TypeError, ValueError):
        raise HTTPException(404, "Collection not found")
    shelf = db.get(Bookshelf, sid)
    if shelf is None or shelf.user_id != user.id:
        raise HTTPException(404, "Collection not found")
    return shelf


def _collection_dict(db: Session, shelf: Bookshelf, *, expanded: bool = True) -> dict:
    works = db.scalars(select(Work).join(BookshelfItem, BookshelfItem.work_id == Work.id)
                       .where(BookshelfItem.shelf_id == shelf.id)).all() if expanded else []
    return {
        "id": f"col_{shelf.id}", "libraryId": _DEFAULT_LIB, "userId": str(shelf.user_id),
        "name": shelf.name, "description": None,
        "cover": None, "coverFullPath": None, "books": [_library_item(w, minified=True) for w in works],
        "createdAt": _ms(shelf.created_at), "lastUpdate": _ms(shelf.updated_at),
    }


def _user_bookshelves(db: Session, user_id: int) -> list[Bookshelf]:
    return list(db.scalars(select(Bookshelf).where(Bookshelf.user_id == user_id)
                           .order_by(Bookshelf.sort_order, Bookshelf.id)).all())


# --------------------------------------------------------------------------------- user payload
def _user_payload(user: User, token: str, db: Session) -> dict:
    return {
        "id": str(user.id), "username": user.username, "type": "admin" if user.role == "admin" else "user",
        "token": token, "mediaProgress": _all_progress(user.id, db),
        "seriesHideFromContinueListening": [], "bookmarks": [],
        "isActive": bool(user.is_active), "isLocked": False,
        "lastSeen": _now_ms(), "createdAt": _ms(getattr(user, "created_at", None)),
        "permissions": {"download": True, "update": True, "delete": user.role == "admin",
                        "upload": False, "accessAllLibraries": True, "accessAllTags": True,
                        "accessExplicitContent": True},
        "librariesAccessible": [], "itemTagsAccessible": [],
    }


def _library_dict(library_id: str) -> dict:
    name, _kinds, _audio = _LIBS[library_id]
    order = list(_LIBS).index(library_id) + 1
    return {
        "id": library_id, "name": name,
        "folders": [{"id": library_id, "fullPath": f"/{library_id}", "libraryId": library_id, "addedAt": 0}],
        "displayOrder": order, "icon": "audiobookshelf", "mediaType": "book", "provider": "audible",
        "settings": {"coverAspectRatio": 1, "disableWatcher": True, "skipMatchingMediaWithAsin": False,
                     "skipMatchingMediaWithIsbn": False, "autoScanCronExpression": None},
        "createdAt": 0, "lastUpdate": _now_ms(),
    }


# ================================================================= unauthenticated bootstrap
@router.get("/status")
def status() -> dict:
    """The probe an ABS app hits BEFORE login. MUST be unauthenticated JSON (not the SPA sign-in HTML,
    else the client reports 'server returned a sign in page instead of the expected data')."""
    return {"app": "audiobookshelf", "serverVersion": "2.8.0", "isInit": True, "language": "en",
            "authMethods": ["local"], "authFormData": {}}


@router.get("/ping")
def ping() -> dict:
    return {"success": True}


@router.get("/healthcheck")
def healthcheck() -> dict:
    return {"success": True}


# socket.io isn't supported (Shelf has no realtime push) — return a clean 404 instead of letting it
# fall through to the SPA HTML, which makes the client's socket layer retry-loop on a bad handshake.
@router.api_route("/socket.io/", methods=["GET", "POST"])
@router.api_route("/socket.io/{rest:path}", methods=["GET", "POST"])
def socketio_unsupported(rest: str = "") -> Response:
    return Response(status_code=404)


@router.post("/login")
def login(payload: dict, request: Request, db: Session = Depends(get_db)) -> dict:
    """ABS login: username + password -> a session token + the ABS user/serverSettings bootstrap.
    Mirrors the web login's brute-force throttle AND admin-approval gate (shared u:/ip: keys)."""
    from ..auth import clear_login_failures, client_ip, record_login_failure, verify_password
    from .auth import _too_many
    uname = (payload.get("username") or "").strip()
    pw = payload.get("password") or ""
    uk, ik = f"u:{uname.lower()}", f"ip:{client_ip(request)}"
    _too_many(uk, ik)
    user = db.scalar(select(User).where(User.username == uname)) if uname else None
    if user is None or not user.is_active or not verify_password(pw, user.password_hash):
        record_login_failure(uk, ik)
        raise HTTPException(401, "Invalid username or password")
    if user.approval_status != "approved":
        raise HTTPException(403, "Your account is pending approval by an administrator.")
    clear_login_failures(uk, ik)
    token = create_session(db, user)
    return {
        "user": _user_payload(user, token, db), "userDefaultLibraryId": _DEFAULT_LIB,
        "serverSettings": {"id": "shelf", "scannerFindCovers": False, "scannerCoverProvider": "",
                           "scannerParseSubtitle": False, "language": "en", "logLevel": 3, "version": "2.8.0"},
        "Source": "shelf",
    }


# ================================================================= authenticated: user
@router.api_route("/api/authorize", methods=["GET", "POST"])
def authorize(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    return {"user": _user_payload(user, request_session_token(request) or "", db),
            "userDefaultLibraryId": _DEFAULT_LIB}


@router.get("/api/me")
def me(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    return _user_payload(user, request_session_token(request) or "", db)


@router.get("/api/me/progress/{item_id}")
def get_progress(item_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    p = _media_progress(user.id, _get_item(db, item_id), db)
    if p is None:
        raise HTTPException(404, "No progress")
    return p


@router.patch("/api/me/progress/{item_id}")
def patch_progress(item_id: str, payload: dict, user: User = Depends(current_user),
                   db: Session = Depends(get_db)) -> dict:
    work = _get_item(db, item_id)
    _write_progress(db, user.id, work, float(payload.get("currentTime") or 0.0),
                    finished=payload.get("isFinished"))
    return _media_progress(user.id, work, db) or {"libraryItemId": str(work.id)}


@router.patch("/api/me/progress/batch/update")
def patch_progress_batch(payload: list[dict], user: User = Depends(current_user),
                         db: Session = Depends(get_db)) -> dict:
    for row in payload or []:
        wid = str(row.get("libraryItemId") or "")
        try:
            work = _get_item(db, wid)
        except HTTPException:
            continue
        _write_progress(db, user.id, work, float(row.get("currentTime") or 0.0),
                        finished=row.get("isFinished"))
    return {"success": True}


@router.get("/api/me/items-in-progress")
def items_in_progress(user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    works = {w.id: w for w in _audio_works(db)}
    states = db.scalars(select(ReadingState).where(
        ReadingState.user_id == user.id, ReadingState.work_id.in_(list(works)),
        ReadingState.audio_updated_at.is_not(None))).all() if works else []
    out = []
    for st in sorted(states, key=lambda s: s.audio_updated_at or _utcnow(), reverse=True):
        w = works.get(st.work_id)
        if w is None:
            continue
        p = _progress_dict(user.id, w, st)
        if not p["isFinished"]:
            out.append(_library_item(w, minified=True))
    return {"libraryItems": out[:24]}


@router.get("/api/me/listening-sessions")
def listening_sessions(user: User = Depends(current_user)) -> dict:
    return {"total": 0, "numPages": 0, "page": 0, "itemsPerPage": 10, "sessions": []}


# ================================================================= libraries
@router.get("/api/libraries")
def libraries(_: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    # Only surface a library that actually has stocked content, so the app doesn't show empty shelves.
    libs = [_library_dict(lid) for lid in _LIBS
            if db.scalar(select(func.count()).select_from(Work).where(
                Work.media_kind.in_(_LIBS[lid][1]), Work.local_path.is_not(None)))]
    return {"libraries": libs or [_library_dict(_DEFAULT_LIB)]}


@router.get("/api/libraries/{library_id}")
def library(library_id: str, include: str = "", _: User = Depends(current_user)) -> dict:
    _lib(library_id)
    d = _library_dict(library_id)
    if "filterdata" in include:   # some clients fetch facets via ?include=filterdata, not /filterdata
        d["filterdata"] = {"authors": [], "genres": [], "tags": [], "series": [], "narrators": [],
                           "languages": []}
    return d


@router.get("/api/libraries/{library_id}/items")
def library_items(library_id: str, _: User = Depends(current_user), db: Session = Depends(get_db),
                  limit: int = Query(0, ge=0), page: int = Query(0, ge=0),
                  sort: str = "", desc: int = 0, filter: str = "") -> dict:
    works, total = _library_page(db, library_id, limit, page * limit if limit else 0,
                                 _filter_conds(filter))
    if desc:
        works = list(reversed(works))
    return {"results": [_library_item(w, minified=True) for w in works], "total": total,
            "limit": limit, "page": page, "sortBy": sort or "media.metadata.title",
            "sortDesc": bool(desc), "filterBy": filter, "mediaType": "book", "minified": True,
            "collapseseries": False, "include": ""}


@router.get("/api/libraries/{library_id}/personalized")
def personalized(library_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[dict]:
    """The home-screen shelves. Missing this is what leaves the app stuck on a loading spinner."""
    _lib(library_id)
    recent, _total = _library_page(db, library_id, 12, 0)
    shelves: list[dict] = []
    if library_id == _DEFAULT_LIB:
        cont = items_in_progress(user, db)["libraryItems"]
        if cont:
            shelves.append({"id": "continue-listening", "label": "Continue Listening",
                            "labelStringKey": "LabelContinueListening", "type": "book", "entities": cont})
    if recent:
        shelves.append({"id": "recently-added", "label": "Recently Added",
                        "labelStringKey": "LabelRecentlyAdded", "type": "book",
                        "entities": [_library_item(w, minified=True) for w in recent]})
    return shelves


@router.get("/api/libraries/{library_id}/filterdata")
def filterdata(library_id: str, _: User = Depends(current_user)) -> dict:
    _lib(library_id)
    return {"authors": [], "genres": [], "tags": [], "series": [], "narrators": [], "languages": []}


@router.get("/api/libraries/{library_id}/authors")
def library_authors(library_id: str, _: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    """The Authors section — one row per distinct author in the library (with book counts)."""
    _n, kinds, _a = _lib(library_id)
    rows = db.execute(
        select(Work.author, func.count()).where(
            Work.media_kind.in_(kinds), Work.local_path.is_not(None),
            Work.author.is_not(None), Work.author != "")
        .group_by(Work.author).order_by(Work.author)).all()
    return {"authors": [{"id": f"aut_{quote(a, safe='')}", "name": a, "numBooks": n,
                         "imagePath": None, "addedAt": 0, "updatedAt": 0} for a, n in rows]}


@router.get("/api/libraries/{library_id}/narrators")
def library_narrators(library_id: str, _: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    _n, kinds, _a = _lib(library_id)
    rows = db.execute(
        select(Work.narrator, func.count()).where(
            Work.media_kind.in_(kinds), Work.local_path.is_not(None),
            Work.narrator.is_not(None), Work.narrator != "")
        .group_by(Work.narrator).order_by(Work.narrator)).all()
    return {"narrators": [{"id": f"nrt_{quote(a, safe='')}", "name": a, "numBooks": n} for a, n in rows]}


@router.get("/api/libraries/{library_id}/genres")
def library_genres(library_id: str, _: User = Depends(current_user)) -> dict:
    # Shelf stores genres as a per-work JSON list; skip the full scan and return an empty section so
    # the client renders it without spinning (rather than 404).
    _lib(library_id)
    return {"genres": []}


@router.get("/api/libraries/{library_id}/stats")
def library_stats(library_id: str, _: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    """Library statistics — what the header shows while it 'loads' (the top spinner)."""
    _n, kinds, _a = _lib(library_id)
    base = (Work.media_kind.in_(kinds), Work.local_path.is_not(None))
    total = db.scalar(select(func.count()).select_from(Work).where(*base)) or 0
    authors = db.scalar(select(func.count(func.distinct(Work.author))).where(*base, Work.author != "")) or 0
    size = db.scalar(select(func.coalesce(func.sum(Work.local_size), 0)).where(*base)) or 0
    return {"totalItems": total, "totalAuthors": authors, "totalGenres": 0, "totalDuration": 0,
            "numAudioTracks": total, "totalSize": int(size), "authorsWithCount": [], "genresWithCount": []}


@router.get("/api/me/bookmarks")
def me_bookmarks(_: User = Depends(current_user)) -> dict:
    return {"bookmarks": []}


@router.get("/api/me/listening-stats")
def listening_stats(_: User = Depends(current_user)) -> dict:
    return {"totalTime": 0, "items": {}, "days": {}, "dayOfWeek": {}, "today": 0, "recentSessions": []}


@router.get("/api/tags")
def tags(_: User = Depends(current_user)) -> dict:
    return {"tags": []}


# ---------------------------------------------------------------------------- search
_EMPTY_SEARCH = {"book": [], "podcast": [], "authors": [], "series": [], "narrators": [], "tags": []}


def _search_payload(db: Session, kinds: tuple[str, ...], q: str) -> dict:
    like = f"%{q.strip()}%"
    base = (Work.media_kind.in_(kinds), Work.local_path.is_not(None))
    items = db.scalars(select(Work).where(*base, or_(Work.title.ilike(like), Work.author.ilike(like)))
                       .order_by(Work.title).limit(25)).all()
    book = [{"libraryItem": _library_item(w, minified=True), "matchKey": "title",
             "matchText": w.title or ""} for w in items]
    authrows = db.execute(select(Work.author, func.count()).where(
        *base, Work.author.ilike(like), Work.author != "").group_by(Work.author).limit(15)).all()
    authors = [{"id": f"aut_{quote(a, safe='')}", "name": a, "numBooks": n} for a, n in authrows]
    return {**_EMPTY_SEARCH, "book": book, "authors": authors}


@router.get("/api/libraries/{library_id}/search")
def library_search(library_id: str, q: str = "", _: User = Depends(current_user),
                   db: Session = Depends(get_db)) -> dict:
    _n, kinds, _a = _lib(library_id)
    return _search_payload(db, kinds, q) if q.strip() else dict(_EMPTY_SEARCH)


@router.get("/api/search")
def global_search(q: str = "", _: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    return _search_payload(db, ("audio", "text", "comic"), q) if q.strip() else dict(_EMPTY_SEARCH)


# ---------------------------------------------------------------------------- author / series detail
@router.get("/api/authors/{author_id}")
def author_detail(author_id: str, include: str = "", _: User = Depends(current_user),
                  db: Session = Depends(get_db)) -> dict:
    name = unquote(author_id[4:]) if author_id.startswith("aut_") else author_id
    items = db.scalars(select(Work).where(Work.author == name, Work.local_path.is_not(None))
                       .order_by(Work.title)).all()
    out = {"id": author_id, "name": name, "description": None, "imagePath": None,
           "addedAt": 0, "updatedAt": 0, "numBooks": len(items),
           "libraryItems": [_library_item(w, minified=True) for w in items] if "items" in include else []}
    if "series" in include:   # the author page groups their books by series
        grouped: dict[str, list[Work]] = {}
        for w in items:
            if w.series:
                grouped.setdefault(w.series, []).append(w)
        out["series"] = [{"id": f"ser_{quote(s, safe='')}", "name": s,
                          "items": [_library_item(w, minified=True) for w in ws]}
                         for s, ws in sorted(grouped.items())]
    return out


@router.get("/api/series/{series_id}")
def series_detail(series_id: str, _: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    name = unquote(series_id[4:]) if series_id.startswith("ser_") else series_id
    items = db.scalars(select(Work).where(Work.series == name, Work.local_path.is_not(None))
                       .order_by(Work.series_position, Work.title)).all()
    books, total_dur = [], 0.0
    for w in items:
        li = _library_item(w, minified=True)
        li["seriesSequence"] = {"sequence": str(w.series_position or "")}
        total_dur += _duration_s(w)
        books.append(li)
    return {"id": series_id, "name": name, "nameIgnorePrefix": name, "type": "series",
            "description": None, "addedAt": 0, "updatedAt": 0, "totalDuration": total_dur,
            "books": books}


@router.get("/api/libraries/{library_id}/series")
def library_series(library_id: str, _: User = Depends(current_user), db: Session = Depends(get_db),
                   limit: int = 0, page: int = 0) -> dict:
    _n, kinds, _a = _lib(library_id)
    rows = db.scalars(select(Work).where(
        Work.media_kind.in_(kinds), Work.local_path.is_not(None),
        Work.series.is_not(None), Work.series != "")
        .order_by(Work.series, Work.series_position, Work.title)).all()
    grouped: dict[str, list[Work]] = {}
    for w in rows:
        grouped.setdefault(w.series, []).append(w)
    names = sorted(grouped)
    total = len(names)
    if limit:
        names = names[page * limit:(page + 1) * limit]
    results = [{"id": f"ser_{quote(name, safe='')}", "name": name, "addedAt": 0, "updatedAt": 0,
                "books": [_library_item(w, minified=True) for w in grouped[name]]} for name in names]
    return {"results": results, "total": total, "limit": limit, "page": page}


@router.get("/api/libraries/{library_id}/collections")
def library_collections(library_id: str, user: User = Depends(current_user),
                        db: Session = Depends(get_db)) -> dict:
    _lib(library_id)
    shelves = _user_bookshelves(db, user.id)
    return {"results": [_collection_dict(db, s) for s in shelves], "total": len(shelves), "limit": 0, "page": 0}


@router.get("/api/libraries/{library_id}/playlists")
def library_playlists(library_id: str, _: User = Depends(current_user),
                      limit: int = 0, page: int = 0) -> dict:
    _lib(library_id)
    return {"results": [], "total": 0, "limit": limit, "page": page}


# ================================================================= items
@router.get("/api/items/{item_id}")
def item(item_id: str, request: Request, _: User = Depends(current_user),
         db: Session = Depends(get_db)) -> dict:
    work = _get_item(db, item_id)
    return _library_item(work, minified=False, db=db, token=request_session_token(request))


@router.get("/api/items/{item_id}/cover")
def item_cover(item_id: str, _: User = Depends(current_user), db: Session = Depends(get_db)):
    work = _get_item(db, item_id)
    url = work.cover_url or ""
    if not url:
        raise HTTPException(404, "No cover")
    # Serve the local cover bytes directly (basename strips traversal) — an ABS client can't follow a
    # redirect to the auth-gated /covers path with its token. External cover URLs need no Shelf auth.
    if url.startswith("/covers/"):
        from ..covers import covers_dir
        p = covers_dir() / os.path.basename(url)
        if not p.is_file():
            raise HTTPException(404, "No cover")
        return FileResponse(str(p))
    return RedirectResponse(url)


# Ebook/comic reader + file/item downloads: serve the Work's original file (epub/mobi/pdf/cbz, or a
# single-file audiobook). {ino} is ignored — a Shelf Work maps to one file. current_user honours the
# bearer header or ?token= on these paths (see auth.request_session_token), so Still's reader and
# offline downloader both authenticate.
@router.get("/api/items/{item_id}/ebook")
@router.get("/api/items/{item_id}/ebook/{ino}")
def item_ebook(item_id: str, ino: str | None = None, _: User = Depends(current_user),
               db: Session = Depends(get_db)):
    return _serve_work_file(_get_item(db, item_id), download=False)


@router.get("/api/items/{item_id}/download")
def item_download(item_id: str, _: User = Depends(current_user), db: Session = Depends(get_db)):
    return _serve_work_file(_get_item(db, item_id), download=True)


@router.get("/api/items/{item_id}/file/{ino}")
def item_file(item_id: str, ino: str, _: User = Depends(current_user), db: Session = Depends(get_db)):
    return _serve_work_file(_get_item(db, item_id), download=False)


@router.get("/api/items/{item_id}/file/{ino}/download")
def item_file_download(item_id: str, ino: str, _: User = Depends(current_user),
                       db: Session = Depends(get_db)):
    return _serve_work_file(_get_item(db, item_id), download=True)


@router.post("/api/items/{item_id}/play")
@router.post("/api/items/{item_id}/play/{episode_id}")
def play(item_id: str, request: Request, payload: dict | None = None, episode_id: str | None = None,
         user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    work = _get_item(db, item_id)
    if work.media_kind != "audio":
        raise HTTPException(400, "Not an audiobook")
    meta = _probe_audio(db, work)
    if meta is None:
        raise HTTPException(409, "Couldn't read this audiobook's audio.")
    token = request_session_token(request)
    total = float(meta.get("total_duration_s", 0.0))
    # Resume position (global seconds) from the caller's saved progress so the session seeks correctly.
    st = db.scalar(select(ReadingState).where(
        ReadingState.user_id == user.id, ReadingState.work_id == work.id))
    cur = _global_pos(meta, st.audio_track or 0, st.audio_pos_s or 0.0)[0] if st else 0.0
    now = _now_ms()
    return {
        "id": f"play-{user.id}-{work.id}-{now}", "userId": str(user.id),
        "libraryId": _library_id_for(work),
        "libraryItemId": str(work.id), "episodeId": None, "mediaType": "book",
        "chapters": _abs_chapters(meta), "audioTracks": _abs_tracks(work.id, meta, token),
        "displayTitle": work.title or "", "displayAuthor": work.author or "",
        "coverPath": f"/api/items/{work.id}/cover" if work.cover_url else None,
        # playMethod MUST be the ABS integer enum (0=DirectPlay), NOT a string — a client comparing it
        # numerically otherwise mis-routes playback. currentTime/startTime/startedAt MUST be present:
        # an ABS player seeks to session.currentTime on load, and `undefined` → NaN → buffers forever.
        "duration": total, "playMethod": 0, "mediaPlayer": "html5",
        "deviceInfo": {}, "serverVersion": "2.2.23", "timeListening": 0.0,
        "startTime": cur, "currentTime": cur, "startedAt": now, "updatedAt": now,
        "sessionLocation": "local", "listeningSessionId": None,
        "mediaMetadata": _metadata(work, minified=True),
    }


# ================================================================= playback sessions
def _work_from_session(db: Session, session_id: str) -> Work | None:
    # session id we minted is "play-{user}-{work}-{ts}".
    parts = session_id.split("-")
    if len(parts) < 3:
        return None
    try:
        return db.get(Work, int(parts[2]))
    except (ValueError, TypeError):
        return None


@router.post("/api/session/{session_id}/sync")
def session_sync(session_id: str, payload: dict, user: User = Depends(current_user),
                 db: Session = Depends(get_db)) -> Response:
    work = _work_from_session(db, session_id)
    if work is not None:
        _write_progress(db, user.id, work, float(payload.get("currentTime") or 0.0))
    return Response(status_code=200)


@router.post("/api/session/{session_id}/close")
def session_close(session_id: str, payload: dict | None = None, user: User = Depends(current_user),
                  db: Session = Depends(get_db)) -> Response:
    work = _work_from_session(db, session_id)
    if work is not None and payload and payload.get("currentTime") is not None:
        _write_progress(db, user.id, work, float(payload.get("currentTime") or 0.0))
    return Response(status_code=200)


# ================================================================= collections  <->  bookshelves
@router.get("/api/collections")
def collections(user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    return {"collections": [_collection_dict(db, s) for s in _user_bookshelves(db, user.id)]}


@router.post("/api/collections")
def create_collection(payload: dict, user: User = Depends(current_user),
                      db: Session = Depends(get_db)) -> dict:
    name = (payload.get("name") or "").strip() or "Untitled"
    shelf = Bookshelf(user_id=user.id, name=name)
    db.add(shelf); db.commit(); db.refresh(shelf)
    for bid in (payload.get("books") or []):
        try:
            wid = int(bid)
        except (TypeError, ValueError):
            continue
        if db.get(Work, wid) is not None:
            db.add(BookshelfItem(shelf_id=shelf.id, work_id=wid))
    db.commit()
    return _collection_dict(db, shelf)


@router.get("/api/collections/{collection_id}")
def get_collection(collection_id: str, user: User = Depends(current_user),
                   db: Session = Depends(get_db)) -> dict:
    return _collection_dict(db, _shelf_from_collection(db, user, collection_id))


@router.patch("/api/collections/{collection_id}")
def update_collection(collection_id: str, payload: dict, user: User = Depends(current_user),
                      db: Session = Depends(get_db)) -> dict:
    shelf = _shelf_from_collection(db, user, collection_id)
    name = (payload.get("name") or "").strip()
    if name:
        shelf.name = name
        db.commit()
    return _collection_dict(db, shelf)


@router.delete("/api/collections/{collection_id}")
def delete_collection(collection_id: str, user: User = Depends(current_user),
                      db: Session = Depends(get_db)) -> dict:
    shelf = _shelf_from_collection(db, user, collection_id)
    db.execute(BookshelfItem.__table__.delete().where(BookshelfItem.shelf_id == shelf.id))
    db.delete(shelf); db.commit()
    return {"success": True}


@router.post("/api/collections/{collection_id}/book")
def collection_add_book(collection_id: str, payload: dict, user: User = Depends(current_user),
                        db: Session = Depends(get_db)) -> dict:
    shelf = _shelf_from_collection(db, user, collection_id)
    _add_to_shelf(db, shelf, payload.get("id"))
    return _collection_dict(db, shelf)


@router.delete("/api/collections/{collection_id}/book/{item_id}")
def collection_remove_book(collection_id: str, item_id: str, user: User = Depends(current_user),
                           db: Session = Depends(get_db)) -> dict:
    shelf = _shelf_from_collection(db, user, collection_id)
    try:
        wid = int(item_id)
    except (TypeError, ValueError):
        wid = -1
    db.execute(BookshelfItem.__table__.delete().where(
        BookshelfItem.shelf_id == shelf.id, BookshelfItem.work_id == wid))
    db.commit()
    return _collection_dict(db, shelf)


@router.post("/api/collections/{collection_id}/batch/add")
def collection_batch_add(collection_id: str, payload: dict, user: User = Depends(current_user),
                         db: Session = Depends(get_db)) -> dict:
    shelf = _shelf_from_collection(db, user, collection_id)
    for bid in (payload.get("books") or []):
        _add_to_shelf(db, shelf, bid)
    return _collection_dict(db, shelf)


@router.post("/api/collections/{collection_id}/batch/remove")
def collection_batch_remove(collection_id: str, payload: dict, user: User = Depends(current_user),
                            db: Session = Depends(get_db)) -> dict:
    shelf = _shelf_from_collection(db, user, collection_id)
    ids = []
    for bid in (payload.get("books") or []):
        try:
            ids.append(int(bid))
        except (TypeError, ValueError):
            pass
    if ids:
        db.execute(BookshelfItem.__table__.delete().where(
            BookshelfItem.shelf_id == shelf.id, BookshelfItem.work_id.in_(ids)))
        db.commit()
    return _collection_dict(db, shelf)


def _add_to_shelf(db: Session, shelf: Bookshelf, book_id) -> None:
    try:
        wid = int(book_id)
    except (TypeError, ValueError):
        return
    if db.get(Work, wid) is None:
        return
    exists = db.scalar(select(BookshelfItem.id).where(
        BookshelfItem.shelf_id == shelf.id, BookshelfItem.work_id == wid))
    if not exists:
        db.add(BookshelfItem(shelf_id=shelf.id, work_id=wid))
        db.commit()


@router.get("/api/playlists")
def playlists(_: User = Depends(current_user)) -> dict:
    return {"playlists": []}


# ================================================================= offline sync + progress mgmt
@router.post("/logout")
def logout(request: Request, db: Session = Depends(get_db)) -> dict:
    """Revoke the bearer session on sign-out (best-effort; an already-invalid token is a no-op)."""
    from ..auth import delete_session
    tok = request_session_token(request)
    if tok:
        try:
            delete_session(db, tok)
        except Exception:  # noqa: BLE001 — logout must always succeed for the client
            pass
    return {}


@router.post("/api/me/sync-local-progress")
def sync_local_progress(payload: dict, user: User = Depends(current_user),
                        db: Session = Depends(get_db)) -> dict:
    """Reconcile progress a user made OFFLINE with the server (last-write-wins on lastUpdate). Returns
    the count we applied + any rows where the SERVER copy is newer (the client should adopt those)."""
    applied, adopt = 0, []
    for lp in (payload.get("localMediaProgresses") or []):
        try:
            work = _get_item(db, str(lp.get("libraryItemId")))
        except HTTPException:
            continue
        incoming = int(lp.get("lastUpdate") or 0)
        st = db.scalar(select(ReadingState).where(
            ReadingState.user_id == user.id, ReadingState.work_id == work.id))
        server = _ms(st.audio_updated_at) if (st and st.audio_updated_at) else 0
        if incoming >= server:
            _write_progress(db, user.id, work, float(lp.get("currentTime") or 0.0),
                            finished=lp.get("isFinished"))
            applied += 1
        else:
            p = _media_progress(user.id, work, db)
            if p:
                adopt.append(p)
    return {"numServerProgressUpdates": applied, "localProgressUpdates": adopt}


@router.post("/api/session/local")
def session_local(payload: dict, user: User = Depends(current_user),
                  db: Session = Depends(get_db)) -> Response:
    """Sync ONE offline playback session (Still's session-based offline path)."""
    try:
        work = _get_item(db, str(payload.get("libraryItemId")))
        _write_progress(db, user.id, work, float(payload.get("currentTime") or 0.0))
    except HTTPException:
        pass
    return Response(status_code=200)


@router.post("/api/session/local-all")
def session_local_all(payload: dict, user: User = Depends(current_user),
                      db: Session = Depends(get_db)) -> dict:
    for s in (payload.get("sessions") or []):
        try:
            work = _get_item(db, str(s.get("libraryItemId")))
            _write_progress(db, user.id, work, float(s.get("currentTime") or 0.0))
        except HTTPException:
            continue
    return {"results": []}


@router.get("/api/session/{session_id}")
def get_open_session(session_id: str, _: User = Depends(current_user)) -> Response:
    # We don't persist open sessions server-side; the client re-plays on a 404.
    return Response(status_code=404)


@router.delete("/api/me/progress/{item_id}")
def delete_progress(item_id: str, user: User = Depends(current_user),
                    db: Session = Depends(get_db)) -> dict:
    """Reset (mark not-started) — clears the ReadingState audio position for this user+item."""
    work = _get_item(db, item_id)
    st = db.scalar(select(ReadingState).where(
        ReadingState.user_id == user.id, ReadingState.work_id == work.id))
    if st is not None:
        st.audio_track, st.audio_pos_s, st.audio_updated_at = 0, 0.0, None
        db.commit()
    return {"success": True}


@router.delete("/api/me/progress/{item_id}/remove-from-continue-listening")
def remove_from_continue(item_id: str, _: User = Depends(current_user)) -> dict:
    # Shelf has no hide-flag; the client removes it optimistically.
    return {"success": True}


@router.delete("/api/me/series/{series_id}/remove-from-continue-listening")
def series_remove_from_continue(series_id: str, _: User = Depends(current_user)) -> dict:
    return {"success": True}


@router.post("/api/items/batch/get")
def items_batch_get(payload: dict, _: User = Depends(current_user),
                    db: Session = Depends(get_db)) -> dict:
    """Hydrate a set of items by id in one call (offline shelves / collection expansion)."""
    out = []
    for iid in (payload.get("libraryItemIds") or []):
        try:
            w = db.get(Work, int(iid))
        except (TypeError, ValueError):
            continue
        if w is not None and w.local_path:
            out.append(_library_item(w, minified=True))
    return {"libraryItems": out}


# ================================================================= misc facets / stubs
@router.get("/api/genres")
def genres(_: User = Depends(current_user)) -> dict:
    return {"genres": []}


@router.get("/api/authors/{author_id}/image")
def author_image(author_id: str) -> Response:
    # Shelf stores no author images — a clean 404 so the client shows its placeholder (not SPA HTML).
    return Response(status_code=404)


@router.patch("/api/me/password")
def change_password(_: User = Depends(current_user)) -> dict:
    return {"success": False, "error": "Password change is not supported via this API."}


# Bookmarks — Shelf has no per-position bookmark store; accept + echo so the UI doesn't hard-fail.
@router.post("/api/me/item/{item_id}/bookmark")
def create_bookmark(item_id: str, payload: dict, _: User = Depends(current_user),
                    db: Session = Depends(get_db)) -> dict:
    work = _get_item(db, item_id)
    t = float(payload.get("time") or 0.0)
    return {"id": f"bm_{item_id}_{int(t)}", "libraryItemId": item_id,
            "title": payload.get("title") or work.title or "", "time": t, "createdAt": _now_ms()}


@router.patch("/api/me/item/{item_id}/bookmark")
def update_bookmark(item_id: str, payload: dict, _: User = Depends(current_user)) -> dict:
    t = float(payload.get("time") or 0.0)
    return {"id": f"bm_{item_id}_{int(t)}", "libraryItemId": item_id,
            "title": payload.get("title") or "", "time": t, "createdAt": _now_ms()}


@router.delete("/api/me/item/{item_id}/bookmark/{at}")
def delete_bookmark(item_id: str, at: str, _: User = Depends(current_user)) -> dict:
    return {"success": True}


# Playlists — Shelf has no playlist model; return valid (non-persisted) shapes so the screens work.
def _empty_playlist(name: str, user_id: int) -> dict:
    return {"id": "pl_0", "libraryId": _DEFAULT_LIB, "userId": str(user_id), "name": name or "Playlist",
            "description": None, "coverPath": None, "items": [],
            "lastUpdate": _now_ms(), "createdAt": _now_ms()}


@router.post("/api/playlists")
def create_playlist(payload: dict, user: User = Depends(current_user)) -> dict:
    return _empty_playlist(payload.get("name") or "Playlist", user.id)


@router.get("/api/playlists/{playlist_id}")
def get_playlist(playlist_id: str, user: User = Depends(current_user)) -> dict:
    return _empty_playlist("Playlist", user.id)


@router.patch("/api/playlists/{playlist_id}")
def update_playlist(playlist_id: str, payload: dict, user: User = Depends(current_user)) -> dict:
    return _empty_playlist(payload.get("name") or "Playlist", user.id)


@router.delete("/api/playlists/{playlist_id}")
def delete_playlist(playlist_id: str, _: User = Depends(current_user)) -> dict:
    return {"success": True}


@router.post("/api/playlists/{playlist_id}/item")
def playlist_add_item(playlist_id: str, user: User = Depends(current_user)) -> dict:
    return _empty_playlist("Playlist", user.id)


@router.delete("/api/playlists/{playlist_id}/item/{library_item_id}")
def playlist_remove_item(playlist_id: str, library_item_id: str,
                         user: User = Depends(current_user)) -> dict:
    return _empty_playlist("Playlist", user.id)


@router.post("/api/playlists/{playlist_id}/batch/add")
def playlist_batch_add(playlist_id: str, user: User = Depends(current_user)) -> dict:
    return _empty_playlist("Playlist", user.id)


@router.post("/api/playlists/{playlist_id}/batch/remove")
def playlist_batch_remove(playlist_id: str, user: User = Depends(current_user)) -> dict:
    return _empty_playlist("Playlist", user.id)
