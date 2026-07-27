"""Service-token admin API — ``/api/admin/*`` (GATE-1).

Shelf's admin user management (``/api/users``) is session-cookie authenticated, so another machine
cannot drive it. This is the same behaviour behind a bearer service token instead: an external
provisioner creates an account when someone is granted access and deactivates it when the grant ends.
Auth is ``app.service_auth`` — service token only, no session fallback, and the surface is disabled
outright when SHELF_SERVICE_TOKENS is unset.

Every route DELEGATES to the session route function it mirrors, so the two surfaces cannot drift:
the same validation, the same uniqueness checks, the same last-admin guards, the same session
revocation on deactivate. Two things are added that a machine caller cannot work without:

  * a READ endpoint (by id and ``?username=``), so a create can be preceded by a read-back and is
    therefore idempotent — without it a lost response leaves an account the caller cannot address; and
  * a 409 whose body carries the EXISTING user's id as a structured field, so the caller never has to
    scrape one out of a message string.

Cloudflare Access is suppressed here — see ``cloudflare.suppressed``.
"""
from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..integrations import cloudflare
from ..models import User
from ..schemas import UserCreate, UserOut, UserUpdate
from ..service_auth import require_service_token
from .auth import create_user, delete_user, list_users, update_user

router = APIRouter(dependencies=[Depends(require_service_token)])


def _actor() -> User:
    """Stand-in for the admin the session routes take as the acting user.

    Id 0 is one no account can hold (``_reserve_user_id`` starts at 1), so the "you cannot do this to
    yourself" guards are inert on a surface that has no self — while "cannot demote/deactivate/delete
    the last admin" still bites. A provisioner must not be able to lock the instance's admins out.
    Transient: never added to the session, so it is never persisted."""
    return User(id=0, username="", password_hash="", role="admin")


def _conflict(db: Session, payload: UserCreate, exc: HTTPException) -> HTTPException:
    """Re-raise the session API's ``{"detail": "Username already taken"}`` with the existing user's id.

    This is the whole point of the 409: it makes a create whose response never arrived safe to repeat.
    The id is a structured field, never something to parse out of ``message``. Anything that isn't a
    resolvable duplicate is handed back untouched."""
    if exc.status_code != 409:
        return exc
    email = (payload.email or "").strip().lower() or None
    # Exactly the lookups create_user's own duplicate checks use, in the same order.
    existing = db.scalar(select(User).where(User.username == payload.username.strip()))
    reason = "username_taken"
    if existing is None and email:
        existing = db.scalar(select(User).where(func.lower(User.email) == email))
        reason = "email_in_use"
    if existing is None:      # the colliding row went away between the check and here
        return exc
    return HTTPException(409, {
        "error": reason,
        "message": exc.detail,
        "id": existing.id,
        "username": existing.username,
        "email": existing.email,
        "is_active": existing.is_active,
    })


@router.get("/admin/users", response_model=list[UserOut])
def service_list_users(username: str | None = None, db: Session = Depends(get_db)) -> list[User]:
    """List users, optionally filtered to one ``username``.

    The filter matches EXACTLY as create's duplicate check does (case-sensitive): a lookup that finds
    nothing must mean a create that won't 409, or a caller would read back an account it is not about
    to collide with and bind itself to somebody else's."""
    users = list_users(_actor(), db)
    return [u for u in users if u.username == username] if username is not None else users


@router.get("/admin/users/{user_id}", response_model=UserOut)
def service_get_user(user_id: int, db: Session = Depends(get_db)) -> User:
    """One user by the id handed out at create. Ids are never reused (``_reserve_user_id``), so a
    stored handle stays valid — or 404s — for the lifetime of the grant; it can never point at
    somebody else's account."""
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(404, "User not found")
    return user


@router.post("/admin/users", response_model=UserOut, status_code=201)
def service_create_user(
    payload: UserCreate, request: Request, background: BackgroundTasks,
    db: Session = Depends(get_db),
) -> User:
    """Create a user and return it — the id at minimum, or the caller has an account it can never
    revoke. ``allowed_categories`` and ``permissions`` stay optional (absent = inherit the instance
    default, which is the operator's own policy) and ``send_invite`` still defaults false, so Shelf
    never emails a plaintext password for an account somebody else's flow created. There is no 18+
    field here on purpose: ``adult_categories`` is the user's OWN opt-in (PUT /api/auth/me/adult),
    and an opt-in made on somebody's behalf is not one."""
    with cloudflare.suppressed():
        try:
            return create_user(payload, request, background, _actor(), db)
        except HTTPException as exc:
            raise _conflict(db, payload, exc) from exc


@router.patch("/admin/users/{user_id}", response_model=UserOut)
def service_update_user(
    user_id: int, payload: UserUpdate, db: Session = Depends(get_db)
) -> User:
    """Update a user — ``is_active: false`` is the revoke, and it drops their live sessions."""
    with cloudflare.suppressed():
        return update_user(user_id, payload, _actor(), db)


@router.delete("/admin/users/{user_id}")
def service_delete_user(
    user_id: int, db: Session = Depends(get_db),
    x_user_delete_secret: str | None = Header(default=None),
) -> dict:
    """HARD-delete a user + everything they own, under the same protection as the session route: when
    SHELF_USER_DELETE_SECRET is set the matching ``X-User-Delete-Secret`` header is required.
    Deactivating (PATCH is_active=false) is the reversible, unprotected alternative."""
    with cloudflare.suppressed():
        return delete_user(user_id, _actor(), db, x_user_delete_secret)
