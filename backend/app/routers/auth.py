"""Authentication + user management API."""
from __future__ import annotations

import ipaddress
import logging
import re
import secrets
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response
from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
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
from ..models import PasswordResetToken, ReadingState, User, UserSession, UserSettings
from ..schemas import (
    AdultAllowedIn, AdultOptInIn, CategoryDefaultIn, ForgotPasswordIn, LoginIn, MeOut,
    PermissionDefaultIn, PermissionInfo, PermissionsMetaOut, RegisterIn, RegisterOut,
    ResetPasswordIn, SetupIn, UserCreate, UserOut, UserUpdate,
)
from .. import config_store

router = APIRouter()
log = logging.getLogger("shelf.auth")

# A pragmatic "looks like an email" check — we store but never verify the address, so this only
# rejects obvious nonsense (no @, no domain dot). Not RFC-5322; deliberately lenient.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_RESET_TOKEN_TTL = timedelta(hours=1)


def _utcnow() -> datetime:
    return datetime.now(UTC)


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
    # Credentials are valid: a self-registered account still awaiting admin approval can't log in.
    # Checked AFTER the password so it never reveals an account exists to a wrong-password guesser.
    if user.approval_status != "approved":
        raise HTTPException(403, "Your account is pending approval by an administrator.")
    clear_login_failures(uk, ik)
    set_session_cookie(response, create_session(db, user), request)
    return user


@router.post("/auth/logout")
def logout(request: Request, response: Response, db: Session = Depends(get_db)) -> dict:
    delete_session(db, request.cookies.get(settings.auth_cookie))
    clear_session_cookie(response)
    return {"ok": True}


# --------------------------------------------------- self-registration + password recovery
def _registration_mode() -> str:
    mode = str(config_store.effective("registration_mode") or "closed").strip().lower()
    return mode if mode in ("closed", "open", "approval") else "closed"


@router.get("/auth/registration-mode")
def registration_mode() -> dict:
    """Public: which self-registration mode is active, so the login page can show/hide signup."""
    return {"mode": _registration_mode()}


@router.post("/auth/register", response_model=RegisterOut)
def register(payload: RegisterIn, request: Request, response: Response,
             db: Session = Depends(get_db)) -> RegisterOut:
    """Self-service signup, gated by ``registration_mode``. Closed → 403. Open → active + logged in.
    Approval → pending (no session) until an admin approves."""
    mode = _registration_mode()
    if mode == "closed":
        raise HTTPException(403, "Self-registration is disabled.")
    ip = client_ip(request)
    _too_many(f"register:{ip}")
    uname = payload.username.strip()
    email = payload.email.strip().lower()
    if not uname:
        raise HTTPException(422, "A username is required.")
    if not _EMAIL_RE.match(email):
        raise HTTPException(422, "Please enter a valid email address.")
    _check_password(payload.password)
    # Duplicate checks. Registration isn't the enumeration-sensitive surface (forgot-password is),
    # and the user must be told their chosen username is taken — so a clear 409 for both. Counts as
    # a throttled attempt so the form can't be hammered to mine which usernames/emails exist.
    if db.scalar(select(User.id).where(User.username == uname)):
        record_login_failure(f"register:{ip}")
        raise HTTPException(409, "That username is already taken.")
    if db.scalar(select(User.id).where(func.lower(User.email) == email)):
        record_login_failure(f"register:{ip}")
        raise HTTPException(409, "That email is already in use.")
    user = User(
        username=uname,
        email=email,
        password_hash=hash_password(payload.password),
        role="user",
        is_active=True,
        approval_status="approved" if mode == "open" else "pending",
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        # A concurrent signup won the UNIQUE(username)/UNIQUE(email) race — fail closed with the same
        # generic 409 the pre-check returns (constraint is the source of truth).
        db.rollback()
        record_login_failure(f"register:{ip}")
        raise HTTPException(409, "That username or email is already in use.")
    db.refresh(user)
    if mode == "open":
        set_session_cookie(response, create_session(db, user), request)
        return RegisterOut(status="ok", user=UserOut.model_validate(user))
    return RegisterOut(status="pending", user=None)


def _prune_reset_tokens(db: Session) -> None:
    """Opportunistic cleanup of expired/used reset tokens (keeps the table small; no scheduler tick
    needed). Best-effort — never blocks the request it rides on."""
    try:
        db.execute(PasswordResetToken.__table__.delete().where(or_(
            PasswordResetToken.expires_at < _utcnow(),
            PasswordResetToken.used_at.isnot(None),
        )))
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()


def _reset_base_url(request: Request) -> str | None:
    """TRUSTED base URL for a reset link emailed to a user, or None when no trusted origin can be
    determined (caller then sends NO email). The link host must NEVER come from the raw, attacker-
    controllable Host header — that's password-reset poisoning (the token would be mailed pointing at
    an attacker domain). Precedence: explicit ``public_base_url`` → a request Host that matches the
    ``allowed_hosts`` allowlist → None."""
    if settings.public_base_url.strip():
        return settings.public_base_url.strip().rstrip("/")
    allowed = [h for h in settings.allowed_hosts if h and h != "*"]
    if allowed:
        scheme = request.url.scheme
        if settings.trust_proxy:
            scheme = request.headers.get("x-forwarded-proto", scheme)
        host = (request.headers.get("host") or "").split(",")[0].strip()
        chosen = host if host in allowed else allowed[0]
        return f"{scheme}://{chosen}".rstrip("/")
    return None  # no public_base_url and allowed_hosts is unrestricted → can't build a safe link


def _safe_send_email(cfg, to: str, subject: str, body: str) -> None:
    """Send a plain-text email, swallowing all errors. Runs as a BackgroundTask so the forgot-password
    response time is identical for known and unknown accounts (no SMTP-latency enumeration oracle)."""
    try:
        from ..kindle import send_message
        send_message(cfg, to, subject, body)
    except Exception:  # noqa: BLE001 — never leak whether the address exists / SMTP failed
        log.exception("forgot-password: failed to send reset email")


@router.post("/auth/forgot-password")
def forgot_password(payload: ForgotPasswordIn, request: Request, background: BackgroundTasks,
                    db: Session = Depends(get_db)) -> dict:
    """Request a password-reset email. ALWAYS returns {"ok": true} regardless of whether the account
    exists (no user enumeration). If a matching account is found AND a trusted public origin is known
    AND SMTP is configured, a single-use token is created and a reset link emailed (after the response,
    so the SMTP round-trip can't be timed to detect account existence)."""
    ip = client_ip(request)
    identifier = payload.identifier.strip()
    _too_many(f"forgot:{ip}", f"forgot:{identifier.lower()}")
    record_login_failure(f"forgot:{ip}", f"forgot:{identifier.lower()}")
    _prune_reset_tokens(db)
    # Match on username OR (case-insensitive) email.
    user = db.scalar(select(User).where(
        (User.username == identifier) | (func.lower(User.email) == identifier.lower())
    ))
    base = _reset_base_url(request)  # None → no trusted origin → send nothing (never trust raw Host)
    if user is not None and user.email and user.is_active and base is not None:
        from ..kindle import app_smtp, smtp_configured
        cfg = app_smtp(db)
        if smtp_configured(cfg):
            token = secrets.token_urlsafe(32)
            db.add(PasswordResetToken(
                user_id=user.id, token=token, expires_at=_utcnow() + _RESET_TOKEN_TTL
            ))
            db.commit()
            link = f"{base}/reset?token={token}"
            background.add_task(
                _safe_send_email, cfg, user.email,
                f"Reset your {settings.app_name} password",
                f"A password reset was requested for your {settings.app_name} account.\n\n"
                f"Open this link to set a new password (valid for 1 hour):\n{link}\n\n"
                "If you didn't request this, you can ignore this email.",
            )
        else:
            log.warning("forgot-password: SMTP not configured; reset email not sent")
    elif user is not None and base is None:
        log.warning("forgot-password: no trusted public_base_url/allowed_hosts set; reset email not sent")
    return {"ok": True}


@router.post("/auth/reset-password")
def reset_password(payload: ResetPasswordIn, request: Request,
                   db: Session = Depends(get_db)) -> dict:
    """Consume a reset token + set a new password. Revokes every existing session for that user."""
    _too_many(f"reset:{client_ip(request)}")
    _check_password(payload.password)
    # Atomically CLAIM the token: mark it used only if it is currently unused and unexpired. rowcount
    # is the gate, so two concurrent requests can't both consume the same single-use token.
    claimed = db.execute(
        update(PasswordResetToken)
        .where(PasswordResetToken.token == payload.token,
               PasswordResetToken.used_at.is_(None),
               PasswordResetToken.expires_at >= _utcnow())
        .values(used_at=_utcnow())
    )
    if claimed.rowcount != 1:
        record_login_failure(f"reset:{client_ip(request)}")
        raise HTTPException(400, "This reset link is invalid or has expired.")
    row = db.scalar(select(PasswordResetToken).where(PasswordResetToken.token == payload.token))
    user = db.get(User, row.user_id) if row else None
    if user is None:
        db.rollback()
        raise HTTPException(400, "This reset link is invalid or has expired.")
    user.password_hash = hash_password(payload.password)
    # A reset must invalidate every existing session (the old credential is gone).
    db.execute(UserSession.__table__.delete().where(UserSession.user_id == user.id))
    db.commit()
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


def _purge_user(db: Session, user: User) -> None:
    """Remove ALL of a user's owned rows + the user. There is no FK cascade (and SQLite FK
    enforcement is off), so anything keyed to this user must be deleted explicitly or it dangles — a
    leftover enabled per-user integration (e.g. Goodreads) would keep getting synced into a
    now-deleted user's orphaned library. Delete bookshelf items before their shelves (FK order);
    only PER-USER integrations (user_id set) are removed — global/admin ones (user_id NULL) are
    left intact. Shared by admin delete + reject (a rejected signup is purged like any user)."""
    from ..models import (
        Bookshelf,
        BookshelfItem,
        Integration,
        LibraryItem,
        Notification,
        NotificationChannel,
    )
    user_id = user.id
    shelf_ids = select(Bookshelf.id).where(Bookshelf.user_id == user_id)
    db.execute(BookshelfItem.__table__.delete().where(BookshelfItem.shelf_id.in_(shelf_ids)))
    db.execute(Bookshelf.__table__.delete().where(Bookshelf.user_id == user_id))
    db.execute(LibraryItem.__table__.delete().where(LibraryItem.user_id == user_id))
    db.execute(Integration.__table__.delete().where(Integration.user_id == user_id))
    db.execute(UserSession.__table__.delete().where(UserSession.user_id == user_id))
    db.execute(ReadingState.__table__.delete().where(ReadingState.user_id == user_id))
    db.execute(UserSettings.__table__.delete().where(UserSettings.user_id == user_id))
    db.execute(PasswordResetToken.__table__.delete().where(
        PasswordResetToken.user_id == user_id))
    # Notifications + the user's own channels (newer tables not covered by the original cleanup): an
    # orphaned enabled channel could keep attempting delivery for a deleted user_id (F15).
    db.execute(Notification.__table__.delete().where(Notification.user_id == user_id))
    db.execute(NotificationChannel.__table__.delete().where(NotificationChannel.user_id == user_id))
    db.delete(user)
    db.commit()


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
    _purge_user(db, user)
    return {"deleted": user_id}


@router.post("/users/{user_id}/approve", response_model=UserOut)
def approve_user(
    user_id: int, _: User = Depends(require_admin), db: Session = Depends(get_db)
) -> User:
    """Approve a pending self-registered user so they can log in."""
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(404, "User not found")
    user.approval_status = "approved"
    db.commit()
    db.refresh(user)
    return user


@router.post("/users/{user_id}/reject")
def reject_user(
    user_id: int, admin: User = Depends(require_admin), db: Session = Depends(get_db)
) -> dict:
    """Reject a pending self-registered user. We DELETE the user (simplest correct behavior — no
    'rejected' tombstone state to manage; the username/email free up for a fresh signup). Same
    full per-user cleanup as delete_user."""
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(404, "User not found")
    if user.approval_status != "pending":
        raise HTTPException(400, "Only a pending user can be rejected.")
    _purge_user(db, user)
    return {"rejected": user_id}


def _admin_count(db: Session) -> int:
    return db.scalar(
        select(func.count(User.id)).where(User.role == "admin", User.is_active.is_(True))
    ) or 0
