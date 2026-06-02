"""Authentication + user management API."""
from __future__ import annotations

import ipaddress
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ..auth import (
    clear_login_failures,
    clear_session_cookie,
    client_ip,
    create_session,
    current_user_optional,
    delete_session,
    hash_password,
    login_retry_after,
    record_login_failure,
    require_admin,
    set_session_cookie,
    settings,
    users_exist,
    verify_password,
)
from ..db import get_db
from ..models import ReadingState, User, UserSession, UserSettings
from ..schemas import LoginIn, MeOut, SetupIn, UserCreate, UserOut, UserUpdate

router = APIRouter()


def _is_public_ip(ip: str) -> bool:
    """True only for a definitively public (globally-routable) client address. Unparseable
    or local/private addresses return False, so tokenless first-admin setup is refused only
    for clients that are clearly on the public internet (fail-open for local/unknown)."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return not (
        addr.is_loopback or addr.is_private or addr.is_link_local
        or addr.is_reserved or addr.is_unspecified
    )


def _check_password(pw: str) -> None:
    if len(pw or "") < settings.min_password_length:
        raise HTTPException(
            400, f"Password must be at least {settings.min_password_length} characters."
        )


def _too_many(*keys: str) -> None:
    wait = login_retry_after(*keys)
    if wait > 0:
        raise HTTPException(
            429, f"Too many attempts — try again in {wait}s.",
            headers={"Retry-After": str(wait)},
        )


@router.get("/auth/me", response_model=MeOut)
def me(user=Depends(current_user_optional), db: Session = Depends(get_db)) -> MeOut:
    return MeOut(
        authenticated=user is not None,
        needs_setup=not users_exist(db),
        user=UserOut.model_validate(user) if user else None,
    )


@router.post("/auth/setup", response_model=UserOut)
def setup(payload: SetupIn, request: Request, response: Response,
          db: Session = Depends(get_db)) -> User:
    """Create the first admin (only when no users exist) and adopt any pre-existing
    library progress + settings so nothing is lost on upgrade."""
    if users_exist(db):
        raise HTTPException(409, "Setup already completed")
    ip = client_ip(request)
    _too_many(f"setup:{ip}")
    # Optional shared-secret gate so an exposed instance can't be claimed by a stranger.
    if settings.setup_token:
        if not secrets.compare_digest(payload.token or "", settings.setup_token):
            record_login_failure(f"setup:{ip}")
            raise HTTPException(403, "A valid setup token is required to create the first admin.")
    elif _is_public_ip(ip):
        # No token configured AND the request is from a public address: refuse, so a stranger
        # can't race to claim admin on a freshly-exposed instance.
        raise HTTPException(
            403,
            "First-admin setup over a non-local connection requires SHELF_SETUP_TOKEN to be "
            "set. Create the admin from localhost, or configure a setup token.",
        )
    _check_password(payload.password)
    admin = User(
        username=payload.username.strip(),
        display_name=(payload.display_name or "").strip() or None,
        password_hash=hash_password(payload.password),
        role="admin",
        is_active=True,
    )
    db.add(admin)
    db.commit()
    db.refresh(admin)
    # Claim legacy global rows (created before multi-user) for the first admin.
    db.execute(update(ReadingState).where(ReadingState.user_id.is_(None)).values(user_id=admin.id))
    db.execute(update(UserSettings).where(UserSettings.user_id.is_(None)).values(user_id=admin.id))
    db.commit()
    set_session_cookie(response, create_session(db, admin), request)
    return admin


@router.post("/auth/login", response_model=UserOut)
def login(payload: LoginIn, request: Request, response: Response,
          db: Session = Depends(get_db)) -> User:
    uname = payload.username.strip()
    uk, ik = f"u:{uname.lower()}", f"ip:{client_ip(request)}"
    _too_many(uk, ik)  # 429 after too many failures (per account + per client IP)
    user = db.scalar(select(User).where(User.username == uname))
    if user is None or not user.is_active or not verify_password(
        payload.password, user.password_hash
    ):
        record_login_failure(uk, ik)
        raise HTTPException(401, "Invalid username or password")
    clear_login_failures(uk, ik)
    set_session_cookie(response, create_session(db, user), request)
    return user


@router.post("/auth/logout")
def logout(request: Request, response: Response, db: Session = Depends(get_db)) -> dict:
    delete_session(db, request.cookies.get(settings.auth_cookie))
    clear_session_cookie(response)
    return {"ok": True}


# ------------------------------------------------------------- admin: user management
@router.get("/users", response_model=list[UserOut])
def list_users(_: User = Depends(require_admin), db: Session = Depends(get_db)) -> list[User]:
    return list(db.scalars(select(User).order_by(User.created_at)).all())


@router.post("/users", response_model=UserOut)
def create_user(
    payload: UserCreate, _: User = Depends(require_admin), db: Session = Depends(get_db)
) -> User:
    if db.scalar(select(User).where(User.username == payload.username.strip())):
        raise HTTPException(409, "Username already taken")
    _check_password(payload.password)
    role = "admin" if payload.role == "admin" else "user"
    user = User(
        username=payload.username.strip(),
        display_name=(payload.display_name or "").strip() or None,
        password_hash=hash_password(payload.password),
        role=role,
        is_active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@router.patch("/users/{user_id}", response_model=UserOut)
def update_user(
    user_id: int, payload: UserUpdate,
    admin: User = Depends(require_admin), db: Session = Depends(get_db),
) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(404, "User not found")
    if payload.password is not None:
        _check_password(payload.password)
        user.password_hash = hash_password(payload.password)
    if payload.display_name is not None:
        user.display_name = payload.display_name.strip() or None
    if payload.role is not None:
        new_role = "admin" if payload.role == "admin" else "user"
        if user.role == "admin" and new_role != "admin" and _admin_count(db) <= 1:
            raise HTTPException(400, "Cannot demote the last admin")
        user.role = new_role
    if payload.is_active is not None:
        if not payload.is_active and user.id == admin.id:
            raise HTTPException(400, "You cannot deactivate yourself")
        if not payload.is_active and user.role == "admin" and _admin_count(db) <= 1:
            raise HTTPException(400, "Cannot deactivate the last admin")
        user.is_active = payload.is_active
        if not payload.is_active:  # revoke their sessions
            db.execute(UserSession.__table__.delete().where(UserSession.user_id == user.id))
    db.commit()
    db.refresh(user)
    return user


@router.delete("/users/{user_id}")
def delete_user(
    user_id: int, admin: User = Depends(require_admin), db: Session = Depends(get_db)
) -> dict:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(404, "User not found")
    if user.id == admin.id:
        raise HTTPException(400, "You cannot delete your own account")
    if user.role == "admin" and _admin_count(db) <= 1:
        raise HTTPException(400, "Cannot delete the last admin")
    # Remove the user's sessions, reading progress, and settings.
    db.execute(UserSession.__table__.delete().where(UserSession.user_id == user_id))
    db.execute(ReadingState.__table__.delete().where(ReadingState.user_id == user_id))
    db.execute(UserSettings.__table__.delete().where(UserSettings.user_id == user_id))
    db.delete(user)
    db.commit()
    return {"deleted": user_id}


def _admin_count(db: Session) -> int:
    return db.scalar(
        select(func.count(User.id)).where(User.role == "admin", User.is_active.is_(True))
    ) or 0
