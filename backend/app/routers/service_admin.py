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

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..auth import client_ip
from ..db import get_db
from ..integrations import cloudflare
from ..models import User
from ..sanitize import log_safe
from ..schemas import UserCreate, UserOut, UserUpdate
from ..service_auth import record as _record_rate_hit, require_service_token
from .auth import create_user, delete_user, list_users, update_user

log = logging.getLogger("shelf.service_admin")

router = APIRouter(dependencies=[Depends(require_service_token)])

# The fields a PROVISIONER must not drive. This surface grants and revokes ACCESS; it is not a way to
# mint a second operator or to seize an existing one. Without this, a provisioning token is a full
# instance-admin credential by two one-step paths: create role=admin then log in at /api/auth/login
# (new accounts are approved+active), or PATCH the operator's own password. Neither is contained by
# the last-admin guards — create admin B, then deactivate admin A.
#
# These are REJECTED, not silently dropped: a caller that asked to reset a password must not be told
# it worked. The reversible revoke (PATCH is_active=false) is untouched, which is the actual job.
_UPDATE_ALLOWED = frozenset(
    {"is_active", "display_name", "email", "permissions", "allowed_categories"}
)


def _refuse_admin_target(db: Session, user_id: int) -> None:
    """A provisioner may not modify or delete an OPERATOR's account.

    Field-level restriction alone is not enough, because ``email`` has to stay writable for the
    legitimate provisioning case — and rewriting an admin's email is a full takeover in three
    requests: PATCH the operator's email to one you control, POST /api/auth/forgot-password with
    their username (it matches on username but mails ``user.email``), then reset the password. Admin
    accounts are the operator's own; they are managed from the Users page, never from here."""
    target = db.get(User, user_id)
    if target is not None and target.role == "admin":
        raise HTTPException(403, "This surface cannot modify an admin account")


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
    db: Session = Depends(get_db), token_id: str = Depends(require_service_token),
) -> User:
    """Create a user and return it — the id at minimum, or the caller has an account it can never
    revoke. ``allowed_categories`` and ``permissions`` stay optional (absent = inherit the instance
    default, which is the operator's own policy) and ``send_invite`` still defaults false, so Shelf
    never emails a plaintext password for an account somebody else's flow created. There is no 18+
    field here on purpose: ``adult_categories`` is the user's OWN opt-in (PUT /api/auth/me/adult),
    and an opt-in made on somebody's behalf is not one.

    ``role`` is pinned to "user": see _UPDATE_ALLOWED — an admin minted here is a login, and a login
    is full instance admin."""
    if payload.role != "user":
        raise HTTPException(403, "This surface cannot create admin accounts")
    with cloudflare.suppressed():
        try:
            user = create_user(payload, request, background, _actor(), db)
        except HTTPException as exc:
            raise _conflict(db, payload, exc) from exc
    log.info("service-admin[%s]: created user id=%s username=%s",
             log_safe(token_id), user.id, log_safe(user.username))
    return user


@router.patch("/admin/users/{user_id}", response_model=UserOut)
def service_update_user(
    user_id: int, payload: UserUpdate, db: Session = Depends(get_db),
    token_id: str = Depends(require_service_token),
) -> User:
    """Update a user — ``is_active: false`` is the revoke, and it drops their live sessions.

    Restricted to _UPDATE_ALLOWED: ``role``/``password``/``username`` are account TAKEOVER, not
    provisioning. Rebuilt rather than mutated so "present even as null" (which update_user reads off
    ``model_fields_set``, e.g. email-clearing) survives the filter exactly as the caller sent it."""
    sent = payload.model_dump(exclude_unset=True)
    if refused := sorted(set(sent) - _UPDATE_ALLOWED):
        raise HTTPException(403, f"This surface cannot change: {', '.join(refused)}")
    _refuse_admin_target(db, user_id)
    with cloudflare.suppressed():
        user = update_user(user_id, UserUpdate(**sent), _actor(), db)
    log.info("service-admin[%s]: updated user id=%s fields=%s",
             log_safe(token_id), user_id, log_safe(sorted(sent)))
    return user


@router.delete("/admin/users/{user_id}")
def service_delete_user(
    user_id: int, request: Request, db: Session = Depends(get_db),
    x_user_delete_secret: str | None = Header(default=None),
    token_id: str = Depends(require_service_token),
) -> dict:
    """HARD-delete a user + everything they own, under the same protection as the session route: when
    SHELF_USER_DELETE_SECRET is set the matching ``X-User-Delete-Secret`` header is required.
    Deactivating (PATCH is_active=false) is the reversible, unprotected alternative.

    A WRONG secret is charged to the token-guessing budget: only a bad *token* did that before, so a
    token holder got the whole per-minute allowance as free guesses against the one control standing
    between them and irreversible deletion."""
    _refuse_admin_target(db, user_id)
    with cloudflare.suppressed():
        try:
            result = delete_user(user_id, _actor(), db, x_user_delete_secret)
        except HTTPException as exc:
            if exc.status_code == 403:
                _record_rate_hit(f"svc-fail:{client_ip(request)}")
                log.warning(
                    "service-admin[%s]: rejected delete of user id=%s (bad delete secret)",
                    log_safe(token_id), user_id,
                )
            raise
    log.info("service-admin[%s]: HARD-deleted user id=%s", log_safe(token_id), user_id)
    return result
