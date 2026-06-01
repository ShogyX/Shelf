"""Authentication: password hashing, server-side sessions, and FastAPI deps.

Self-contained (stdlib pbkdf2 — no extra dependency). Sessions are random tokens
stored in `user_sessions` and carried in an httpOnly cookie.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta

from fastapi import Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import get_settings
from .db import get_db
from .models import User, UserSession

settings = get_settings()
_ITERATIONS = 200_000


def _utcnow() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------- passwords
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _ITERATIONS)
    return f"pbkdf2_sha256${_ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt_hex, hash_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iters)
        )
        return hmac.compare_digest(dk.hex(), hash_hex)
    except Exception:
        return False


# ---------------------------------------------------------------------- sessions
def create_session(db: Session, user: User) -> str:
    token = secrets.token_urlsafe(32)
    db.add(UserSession(
        token=token, user_id=user.id, expires_at=_utcnow() + timedelta(days=settings.session_days)
    ))
    db.commit()
    return token


def session_user(db: Session, token: str | None) -> User | None:
    if not token:
        return None
    s = db.scalar(select(UserSession).where(UserSession.token == token))
    if s is None:
        return None
    exp = s.expires_at
    if exp is not None and exp.tzinfo is None:
        exp = exp.replace(tzinfo=UTC)
    if exp is not None and exp < _utcnow():
        db.delete(s)
        db.commit()
        return None
    user = db.get(User, s.user_id)
    if user is None or not user.is_active:
        return None
    return user


def delete_session(db: Session, token: str | None) -> None:
    if not token:
        return
    s = db.scalar(select(UserSession).where(UserSession.token == token))
    if s is not None:
        db.delete(s)
        db.commit()


def users_exist(db: Session) -> bool:
    return db.scalar(select(User.id).limit(1)) is not None


# ----------------------------------------------------------------------- cookies
def set_session_cookie(resp: Response, token: str) -> None:
    resp.set_cookie(
        settings.auth_cookie, token, httponly=True, samesite="lax",
        secure=settings.cookie_secure, max_age=settings.session_days * 86400, path="/",
    )


def clear_session_cookie(resp: Response) -> None:
    resp.delete_cookie(settings.auth_cookie, path="/")


# ------------------------------------------------------------------- dependencies
def current_user_optional(request: Request, db: Session = Depends(get_db)) -> User | None:
    return session_user(db, request.cookies.get(settings.auth_cookie))


def current_user(user: User | None = Depends(current_user_optional)) -> User:
    if user is None:
        raise HTTPException(401, "Not authenticated")
    return user


def require_admin(user: User = Depends(current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(403, "Admin privileges required")
    return user


def require_auth(_: User = Depends(current_user)) -> None:
    """Router-level gate: 401s unauthenticated requests without injecting the user."""
    return None
