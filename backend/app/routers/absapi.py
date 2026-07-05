"""Audiobookshelf-compatible API surface.

Lets an Audiobookshelf listening companion app (e.g. "Still") connect to Shelf natively: log in, browse
the audiobook library, open an item, stream it, and sync listening progress. We map Shelf's audiobook
Works (media_kind="audio") onto the ABS `libraryItem` / `mediaProgress` shapes and reuse Shelf's own
audio probe + streaming endpoints. Auth is Shelf's ordinary session token, presented as an ABS bearer
token (issued by POST /login); see auth.request_session_token, which also accepts it as ?token= on the
media URLs an ABS client builds.

Scope is the MVP that makes the browse -> open -> play -> sync flow work end to end; podcasts,
collections, playlists, ebooks and server-admin endpoints are intentionally out of scope.
"""
from __future__ import annotations

import os
import time

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import create_session, current_user, request_session_token
from ..db import get_db
from ..models import ReadingState, User, Work
from .delivery import _global_pos, _probe_audio

router = APIRouter()

# The whole audiobook pool is one ABS "library". A fixed id keeps it stable across restarts (ABS
# clients cache the library id + a per-library default).
_LIB_ID = "shelf-audiobooks"
_FINISHED_AT = 0.985   # progress fraction at/above which a title reads as finished (ABS convention)


def _ms(dt) -> int:
    """A datetime -> epoch milliseconds (ABS uses ms). None -> 0."""
    if dt is None:
        return 0
    try:
        return int(dt.timestamp() * 1000)
    except (OSError, OverflowError, ValueError):
        return 0


def _now_ms() -> int:
    return int(time.time() * 1000)


def _audio_works(db: Session) -> list[Work]:
    return db.scalars(
        select(Work).where(Work.media_kind == "audio", Work.local_path.is_not(None))
        .order_by(Work.title)).all()


def _duration_s(work: Work) -> float:
    """Total duration from the cached probe manifest, without re-probing (0 if never probed)."""
    meta = work.audio_meta if isinstance(work.audio_meta, dict) else None
    if meta and isinstance(meta.get("total_duration_s"), (int, float)):
        return float(meta["total_duration_s"])
    return 0.0


def _metadata(work: Work, *, minified: bool) -> dict:
    """ABS media.metadata. Minified variants (list rows) use the *Name string fields; the full item
    also carries the authors[]/narrators[]/series[] object arrays."""
    author = work.author or ""
    narrator = work.narrator or ""
    md = {
        "title": work.title or "",
        "titleIgnorePrefix": work.title or "",
        "subtitle": None,
        "authorName": author,
        "narratorName": narrator,
        "seriesName": work.series or "",
        "genres": list(work.genres or []) if isinstance(work.genres, list) else [],
        "publishedYear": str(work.year) if work.year else None,
        "publisher": work.publisher or None,
        "description": work.description or "",
        "isbn": None,
        "asin": None,
        "language": work.language or None,
        "explicit": False,
    }
    if not minified:
        md["authors"] = [{"id": f"aut_{work.id}", "name": author}] if author else []
        md["narrators"] = [narrator] if narrator else []
        md["series"] = ([{"id": f"ser_{work.series_id or work.id}", "name": work.series,
                          "sequence": (str(work.series_position) if work.series_position else "")}]
                         if work.series else [])
    return md


def _abs_tracks(work_id: int, meta: dict | None, token: str | None) -> list[dict]:
    """ABS audioTracks: one per Shelf stream track, each a self-contained URL the client GETs with
    range. The token rides as ?token= so the ABS client (no Shelf cookie) still authenticates."""
    if not meta:
        return []
    q = f"?token={token}" if token else ""
    out, offset = [], 0.0
    for t in meta["tracks"]:
        dur = float(t["duration_s"])
        out.append({
            "index": t["index"],
            "startOffset": offset,
            "duration": dur,
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
    # ABS wants each chapter's end = next chapter's start (last = total duration).
    total = float(meta.get("total_duration_s", 0.0))
    for i in range(len(out)):
        out[i]["end"] = out[i + 1]["start"] if i + 1 < len(out) else total
    return out


def _library_item(work: Work, *, minified: bool, db: Session | None = None,
                  token: str | None = None) -> dict:
    """Full or minified ABS libraryItem for one audiobook Work."""
    dur = _duration_s(work)
    added = _ms(work.created_at)
    media: dict = {
        "libraryItemId": str(work.id),
        "metadata": _metadata(work, minified=minified),
        "coverPath": f"/api/items/{work.id}/cover" if work.cover_url else None,
        "tags": [],
        "duration": dur,
        "size": work.local_size or 0,
    }
    if minified:
        media.update({"numTracks": 1, "numAudioFiles": 1,
                      "numChapters": len(work.audio_meta.get("chapters", [])) if isinstance(work.audio_meta, dict) else 0,
                      "ebookFileFormat": None})
    else:
        meta = _probe_audio(db, work) if db is not None else (work.audio_meta if isinstance(work.audio_meta, dict) else None)
        media.update({
            "audioFiles": [], "ebookFile": None,
            "chapters": _abs_chapters(meta),
            "tracks": _abs_tracks(work.id, meta, token),
        })
        if meta and not dur:
            media["duration"] = float(meta.get("total_duration_s", 0.0))
    return {
        "id": str(work.id), "ino": str(work.id), "libraryId": _LIB_ID, "folderId": _LIB_ID,
        "path": work.local_path or "", "relPath": work.local_path or "", "isFile": True,
        "mtimeMs": 0, "ctimeMs": 0, "birthtimeMs": 0, "addedAt": added, "updatedAt": _ms(work.last_update_at) or added,
        "isMissing": False, "isInvalid": False, "mediaType": "book",
        "media": media, "libraryFiles": [], "numFiles": 1, "size": work.local_size or 0,
    }


def _progress_dict(user_id: int, work: Work, st: ReadingState) -> dict:
    """Build one ABS mediaProgress from a (user, work) ReadingState that has audio progress."""
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
    """ABS mediaProgress for (user, work), or None if the user has no listening progress on it."""
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


def _user_payload(user: User, token: str, db: Session) -> dict:
    return {
        "id": str(user.id), "username": user.username, "type": "admin" if user.role == "admin" else "user",
        "token": token, "mediaProgress": _all_progress(user.id, db),
        "seriesHideFromContinueListening": [], "bookmarks": [],
        "isActive": bool(user.is_active), "isLocked": False,
        "lastSeen": _now_ms(), "createdAt": _ms(getattr(user, "created_at", None)),
        "permissions": {"download": True, "update": user.role == "admin", "delete": user.role == "admin",
                        "upload": False, "accessAllLibraries": True, "accessAllTags": True,
                        "accessExplicitContent": True},
        "librariesAccessible": [], "itemTagsAccessible": [],
    }


# --------------------------------------------------------------------- unauthenticated bootstrap
@router.get("/status")
def status() -> dict:
    """The probe an ABS app hits BEFORE login to confirm this is an Audiobookshelf server and learn
    its auth methods. MUST be unauthenticated and return JSON — if it 401s or falls through to the SPA
    sign-in HTML, the client reports "server returned a sign in page instead of the expected data"."""
    return {
        "app": "audiobookshelf",
        "serverVersion": "2.8.0",
        "isInit": True,
        "language": "en",
        "authMethods": ["local"],
        "authFormData": {},
    }


@router.get("/ping")
def ping() -> dict:
    return {"success": True}


@router.get("/healthcheck")
def healthcheck() -> dict:
    return {"success": True}


@router.post("/login")
def login(payload: dict, request: Request, db: Session = Depends(get_db)) -> dict:
    """ABS login: username + password -> a session token + the ABS user/serverSettings bootstrap.
    Mirrors the web login's brute-force throttle AND admin-approval gate, sharing the SAME u:/ip: keys,
    so this surface can neither be brute-forced unbounded nor used to bypass approval (both would
    otherwise be possible since request_session_token honours the issued token app-wide)."""
    from ..auth import clear_login_failures, client_ip, record_login_failure, verify_password
    from .auth import _too_many
    uname = (payload.get("username") or "").strip()
    pw = payload.get("password") or ""
    uk, ik = f"u:{uname.lower()}", f"ip:{client_ip(request)}"
    _too_many(uk, ik)   # 429 after too many failures (per account + per client IP) — shared with web login
    user = db.scalar(select(User).where(User.username == uname)) if uname else None
    if user is None or not user.is_active or not verify_password(pw, user.password_hash):
        record_login_failure(uk, ik)
        raise HTTPException(401, "Invalid username or password")
    # Valid credentials, but a self-registered account still awaiting approval can't log in (checked
    # after the password so it never reveals the account exists to a wrong-password guesser).
    if user.approval_status != "approved":
        raise HTTPException(403, "Your account is pending approval by an administrator.")
    clear_login_failures(uk, ik)
    token = create_session(db, user)
    return {
        "user": _user_payload(user, token, db),
        "userDefaultLibraryId": _LIB_ID,
        "serverSettings": {"id": "shelf", "scannerFindCovers": False, "scannerCoverProvider": "",
                           "scannerParseSubtitle": False, "language": "en", "logLevel": 3, "version": "2.8.0"},
        "Source": "shelf",
    }


# --------------------------------------------------------------------- authenticated
@router.get("/api/me")
def me(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    return _user_payload(user, request_session_token(request) or "", db)


@router.get("/api/authorize")
def authorize(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    """ABS clients call this after they already hold a token, to re-bootstrap the user object."""
    return {"user": _user_payload(user, request_session_token(request) or "", db),
            "userDefaultLibraryId": _LIB_ID}


@router.get("/api/libraries")
def libraries(_: User = Depends(current_user)) -> dict:
    return {"libraries": [{
        "id": _LIB_ID, "name": "Audiobooks",
        "folders": [{"id": _LIB_ID, "fullPath": "/audiobooks", "libraryId": _LIB_ID, "addedAt": 0}],
        "displayOrder": 1, "icon": "audiobookshelf", "mediaType": "book", "provider": "audible",
        "settings": {"coverAspectRatio": 1, "disableWatcher": True, "skipMatchingMediaWithAsin": False,
                     "skipMatchingMediaWithIsbn": False, "autoScanCronExpression": None},
        "createdAt": 0, "lastUpdate": _now_ms(),
    }]}


@router.get("/api/libraries/{library_id}")
def library(library_id: str, _: User = Depends(current_user)) -> dict:
    return libraries(_)["libraries"][0]


@router.get("/api/libraries/{library_id}/items")
def library_items(library_id: str, _: User = Depends(current_user), db: Session = Depends(get_db),
                  limit: int = Query(0, ge=0), page: int = Query(0, ge=0),
                  sort: str = "", desc: int = 0, minified: int = 1) -> dict:
    works = _audio_works(db)
    if desc:
        works = list(reversed(works))
    total = len(works)
    if limit:
        start = page * limit
        works = works[start:start + limit]
    results = [_library_item(w, minified=True) for w in works]
    return {"results": results, "total": total, "limit": limit, "page": page,
            "sortBy": sort or "media.metadata.title", "sortDesc": bool(desc), "filterBy": "",
            "mediaType": "book", "minified": True, "collapseseries": False, "include": ""}


@router.get("/api/items/{item_id}")
def item(item_id: str, request: Request, _: User = Depends(current_user),
         db: Session = Depends(get_db)) -> dict:
    work = _get_audio(db, item_id)
    return _library_item(work, minified=False, db=db, token=request_session_token(request))


@router.get("/api/items/{item_id}/cover")
def item_cover(item_id: str, _: User = Depends(current_user), db: Session = Depends(get_db)):
    work = _get_audio(db, item_id)
    url = work.cover_url or ""
    if not url:
        raise HTTPException(404, "No cover")
    # Serve the local cover file BYTES directly (basename strips any traversal) rather than redirecting
    # to the auth-gated /covers static path — an ABS client following a redirect can't re-present the
    # token, so a redirect would 401. An external cover URL (rare) needs no Shelf auth, so redirect it.
    if url.startswith("/covers/"):
        from ..covers import covers_dir
        p = covers_dir() / os.path.basename(url)
        if not p.is_file():
            raise HTTPException(404, "No cover")
        return FileResponse(str(p))
    return RedirectResponse(url)


@router.post("/api/items/{item_id}/play")
def play(item_id: str, request: Request, payload: dict | None = None,
         user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    """Open a playback session: the audioTracks the client streams, plus chapters + display metadata.
    Stateless here (Shelf tracks position via /api/me/progress) — we return a session-shaped object."""
    work = _get_audio(db, item_id)
    meta = _probe_audio(db, work)
    if meta is None:
        raise HTTPException(409, "Couldn't read this audiobook's audio.")
    token = request_session_token(request)
    total = float(meta.get("total_duration_s", 0.0))
    return {
        "id": f"play-{user.id}-{work.id}-{_now_ms()}", "userId": str(user.id),
        "libraryItemId": str(work.id), "episodeId": None, "mediaType": "book",
        "chapters": _abs_chapters(meta), "audioTracks": _abs_tracks(work.id, meta, token),
        "displayTitle": work.title or "", "displayAuthor": work.author or "",
        "coverPath": f"/api/items/{work.id}/cover" if work.cover_url else None,
        "duration": total, "playMethod": "directPlay", "mediaPlayer": "html5",
        "sessionLocation": "local", "listeningSessionId": None,
        "mediaMetadata": _metadata(work, minified=True),
    }


@router.patch("/api/me/progress/{item_id}")
def update_progress(item_id: str, payload: dict, user: User = Depends(current_user),
                    db: Session = Depends(get_db)) -> dict:
    """Persist a listening position from the ABS client. ABS sends a single global `currentTime`; map
    it back to Shelf's (track, offset-within-track) using the probed track durations."""
    work = _get_audio(db, item_id)
    meta = work.audio_meta if isinstance(work.audio_meta, dict) else _probe_audio(db, work)
    cur = float(payload.get("currentTime") or 0.0)
    track, pos = _global_to_track(meta, cur)
    st = db.scalar(select(ReadingState).where(
        ReadingState.user_id == user.id, ReadingState.work_id == work.id))
    if st is None:
        st = ReadingState(user_id=user.id, work_id=work.id)
        db.add(st)
    st.audio_track = track
    st.audio_pos_s = pos
    from ..models import _utcnow
    st.audio_updated_at = _utcnow()
    db.commit()
    return _media_progress(user.id, work, db) or {"libraryItemId": str(work.id), "currentTime": cur}


# --------------------------------------------------------------------- helpers
def _get_audio(db: Session, item_id: str) -> Work:
    try:
        wid = int(item_id)
    except (TypeError, ValueError):
        raise HTTPException(404, "Item not found")
    work = db.get(Work, wid)
    if work is None or work.media_kind != "audio":
        raise HTTPException(404, "Item not found")
    return work


def _global_to_track(meta: dict | None, current_s: float) -> tuple[int, float]:
    """Inverse of delivery._global_pos: a global position -> (track index, offset within that track).
    Single-file audiobooks have one track, so this is just (that index, current_s)."""
    if not meta or not meta.get("tracks"):
        return 0, max(0.0, current_s)
    offset = 0.0
    for t in meta["tracks"]:
        dur = float(t["duration_s"])
        if current_s < offset + dur or t is meta["tracks"][-1]:
            return t["index"], max(0.0, current_s - offset)
        offset += dur
    return meta["tracks"][-1]["index"], 0.0
