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
from ..schemas import (
    AdultAllowedIn, AdultOptInIn, CategoryDefaultIn, LoginIn, MeOut, PermissionDefaultIn,
    PermissionInfo, PermissionsMetaOut, SetupIn, UserCreate, UserOut, UserUpdate,
)
from .. import config_store

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


def _looks_proxied_untrusted(request: Request) -> bool:
    """A request that carries proxy-forwarding headers while ``trust_proxy`` is OFF: we deliberately
    DON'T trust those headers, so request.client.host is the proxy (commonly loopback/private) —
    the apparent-local IP cannot be taken to mean the client is physically local. Used to fail the
    tokenless first-admin setup closed in that case."""
    if settings.trust_proxy:
        return False
    return bool(
        request.headers.get("x-forwarded-for")
        or request.headers.get("cf-connecting-ip")
        or request.headers.get("forwarded")
    )


def _check_password(pw: str) -> None:
    if len(pw or "") < config_store.effective("min_password_length"):
        raise HTTPException(
            400, f"Password must be at least {config_store.effective("min_password_length")} characters."
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
    from ..ingestion.catalog import (
        effective_adult_categories, effective_categories, get_adult_allowed,
    )
    from ..permissions import effective_permissions
    return MeOut(
        authenticated=user is not None,
        needs_setup=not users_exist(db),
        user=UserOut.model_validate(user) if user else None,
        allowed_categories=effective_categories(db, user) if user else [],
        permissions=effective_permissions(db, user) if user else [],
        adult_allowed_categories=get_adult_allowed(db) if user else [],
        # Resolved set the viewer actually sees (inherit→full gate by default); drives the opt-in chips.
        adult_categories=effective_adult_categories(db, user) if user else [],
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
    elif _is_public_ip(ip) or _looks_proxied_untrusted(request):
        # No token configured AND the request is either from a public address OR arrived through a
        # proxy we don't trust (forwarding headers present, trust_proxy off → request.client.host
        # is the PROXY, so a private-looking IP can't be trusted to mean 'physically local').
        # Fail closed so a stranger can't race to claim admin on a freshly-exposed instance.
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
    # A work can carry several legacy global (NULL-user) reading_states — duplicates from before
    # the per-user migration. Claiming them ALL for the admin would make two (admin, work) rows and
    # violate uq_reading_user_work, aborting setup. De-dup to ONE per work first (keep the
    # furthest-progressed: last_chapter_id DESC puts NULLs last under SQLite, then newest id).
    from sqlalchemy import text
    db.execute(text(
        "DELETE FROM reading_states WHERE user_id IS NULL AND id NOT IN ("
        " SELECT id FROM ("
        "  SELECT id, ROW_NUMBER() OVER (PARTITION BY work_id"
        "   ORDER BY last_chapter_id DESC, id DESC) AS rn"
        "  FROM reading_states WHERE user_id IS NULL"
        " ) WHERE rn = 1)"
    ))
    # Claim the (now-unique-per-work) legacy global rows for the first admin.
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
    from ..ingestion.catalog import _clean_categories
    from ..permissions import clean_permissions
    role = "admin" if payload.role == "admin" else "user"
    user = User(
        username=payload.username.strip(),
        display_name=(payload.display_name or "").strip() or None,
        password_hash=hash_password(payload.password),
        role=role,
        is_active=True,
        allowed_categories=(
            _clean_categories(payload.allowed_categories)
            if payload.allowed_categories is not None else None
        ),
        permissions=(
            clean_permissions(payload.permissions)
            if payload.permissions is not None else None
        ),
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
    revoke_sessions = False
    if payload.password is not None:
        _check_password(payload.password)
        user.password_hash = hash_password(payload.password)
        # A forced password reset must invalidate every existing session — a stale or
        # compromised session must not keep access after the credential it rode in on changed.
        revoke_sessions = True
    if payload.display_name is not None:
        user.display_name = payload.display_name.strip() or None
    if payload.role is not None:
        new_role = "admin" if payload.role == "admin" else "user"
        if user.role == "admin" and new_role != "admin" and _admin_count(db) <= 1:
            raise HTTPException(400, "Cannot demote the last admin")
        if user.role == "admin" and new_role != "admin":
            # Demotion: existing sessions were minted with admin scope — revoke so a held
            # session can't keep stale-admin access (the user just logs in again as a user).
            revoke_sessions = True
        user.role = new_role
    if revoke_sessions:
        db.execute(UserSession.__table__.delete().where(UserSession.user_id == user.id))
    if payload.is_active is not None:
        if not payload.is_active and user.id == admin.id:
            raise HTTPException(400, "You cannot deactivate yourself")
        if not payload.is_active and user.role == "admin" and _admin_count(db) <= 1:
            raise HTTPException(400, "Cannot deactivate the last admin")
        user.is_active = payload.is_active
        if not payload.is_active:  # revoke their sessions
            db.execute(UserSession.__table__.delete().where(UserSession.user_id == user.id))
    # 'allowed_categories' present (even null) → set the cap; null resets to the global default.
    if "allowed_categories" in payload.model_fields_set:
        from ..ingestion.catalog import _clean_categories
        user.allowed_categories = (
            _clean_categories(payload.allowed_categories)
            if payload.allowed_categories is not None else None
        )
    # 'permissions' present (even null) → set the capability set; null resets to the global default.
    if "permissions" in payload.model_fields_set:
        from ..permissions import clean_permissions
        user.permissions = (
            clean_permissions(payload.permissions)
            if payload.permissions is not None else None
        )
    db.commit()
    db.refresh(user)
    return user


@router.get("/users/category-default")
def get_category_default(_: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict:
    """The default category cap applied to normal users who have no per-user cap. null = all."""
    from ..ingestion.catalog import MEDIA_CATEGORIES, get_default_categories
    return {"categories": get_default_categories(db), "all": list(MEDIA_CATEGORIES)}


@router.put("/users/category-default")
def set_category_default(
    payload: CategoryDefaultIn, _: User = Depends(require_admin), db: Session = Depends(get_db)
) -> dict:
    """Set (or clear with null) the normal-user default category cap."""
    from ..ingestion.catalog import set_default_categories
    return {"categories": set_default_categories(db, payload.categories)}


@router.get("/users/adult-allowed")
def get_adult_allowed_categories(
    _: User = Depends(require_admin), db: Session = Depends(get_db)
) -> dict:
    """The global 18+ gate: which Index categories MAY surface adult content. Empty = off."""
    from ..ingestion.catalog import MEDIA_CATEGORIES, get_adult_allowed
    return {"categories": get_adult_allowed(db), "all": list(MEDIA_CATEGORIES)}


@router.put("/users/adult-allowed")
def set_adult_allowed_categories(
    payload: AdultAllowedIn, _: User = Depends(require_admin), db: Session = Depends(get_db)
) -> dict:
    """Set the global 18+ gate (admin). Categories not listed never show adult content to anyone."""
    from ..ingestion.catalog import set_adult_allowed
    return {"categories": set_adult_allowed(db, payload.categories or [])}


@router.put("/auth/me/adult")
def set_my_adult_categories(
    payload: AdultOptInIn, user: User = Depends(current_user_optional),
    db: Session = Depends(get_db),
) -> dict:
    """A user opts into 18+ content per category (self-service). Bounded by the admin gate at read
    time, so opting into a category the admin later locks simply shows nothing."""
    if user is None:
        raise HTTPException(401, "Authentication required")
    from ..ingestion.catalog import _clean_categories, effective_adult_categories
    user.adult_categories = _clean_categories(payload.categories or [])
    db.commit()
    return {
        "adult_categories": user.adult_categories,
        "effective": effective_adult_categories(db, user),
    }


@router.get("/users/permissions-meta", response_model=PermissionsMetaOut)
def permissions_meta(_: User = Depends(require_admin), db: Session = Depends(get_db)) -> PermissionsMetaOut:
    """Every grantable capability (key + label), the current global default for new users, and the
    built-in baseline. Powers the admin Users permission editor."""
    from .. import permissions as perms
    return PermissionsMetaOut(
        all=[PermissionInfo(key=k, label=v) for k, v in perms.PERMISSIONS.items()],
        default=perms.get_default_permissions(db) if perms.get_default_permissions(db) is not None
        else list(perms.DEFAULT_PERMISSIONS),
        baseline=list(perms.DEFAULT_PERMISSIONS),
    )


@router.put("/users/permission-default")
def set_permission_default(
    payload: PermissionDefaultIn, _: User = Depends(require_admin), db: Session = Depends(get_db)
) -> dict:
    """Set (or reset to the baseline with null) the normal-user default permission set."""
    from ..permissions import set_default_permissions
    return {"permissions": set_default_permissions(db, payload.permissions)}


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
    # Remove ALL of the user's owned rows. There is no FK cascade (and SQLite FK enforcement is off),
    # so anything keyed to this user must be deleted explicitly or it dangles — a leftover enabled
    # per-user integration (e.g. Goodreads) would keep getting synced into a now-deleted user's
    # orphaned library. Delete bookshelf items before their shelves (FK order); only PER-USER
    # integrations (user_id set) are removed — global/admin ones (user_id NULL) are left intact.
    from ..models import Bookshelf, BookshelfItem, Integration, LibraryItem
    shelf_ids = select(Bookshelf.id).where(Bookshelf.user_id == user_id)
    db.execute(BookshelfItem.__table__.delete().where(BookshelfItem.shelf_id.in_(shelf_ids)))
    db.execute(Bookshelf.__table__.delete().where(Bookshelf.user_id == user_id))
    db.execute(LibraryItem.__table__.delete().where(LibraryItem.user_id == user_id))
    db.execute(Integration.__table__.delete().where(Integration.user_id == user_id))
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
