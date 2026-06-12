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


# --------------------------------------------------------------- client IP + cookies
def client_ip(request: Request) -> str:
    """Best-effort real client IP. Only trusts forwarded headers behind a proxy
    (cloudflared on localhost), so they can't be spoofed by direct connections."""
    if settings.trust_proxy:
        cf = request.headers.get("cf-connecting-ip")
        if cf:
            return cf.strip()
        xff = request.headers.get("x-forwarded-for")
        if xff:
            return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _is_secure(request: Request) -> bool:
    if settings.cookie_secure:
        return True
    if settings.trust_proxy:
        proto = request.headers.get("x-forwarded-proto", request.url.scheme)
        return proto == "https"
    return request.url.scheme == "https"


def set_session_cookie(resp: Response, token: str, request: Request | None = None) -> None:
    samesite = settings.cookie_samesite if settings.cookie_samesite in ("lax", "strict", "none") \
        else "lax"
    secure = _is_secure(request) if request is not None else settings.cookie_secure
    resp.set_cookie(
        settings.auth_cookie, token, httponly=True, samesite=samesite,
        secure=secure or samesite == "none", max_age=settings.session_days * 86400, path="/",
    )


def clear_session_cookie(resp: Response) -> None:
    resp.delete_cookie(settings.auth_cookie, path="/")


# ---------------------------------------------------- brute-force login throttling
# NOTE: in-process state — correct for the supported single-worker deployment (install.sh runs
# one uvicorn worker). A multi-worker deployment would multiply the attempt cap by worker count;
# move this to a DB table before ever enabling --workers > 1.
import threading  # noqa: E402

_fail_lock = threading.Lock()
_fail_log: dict[str, list[float]] = {}
# Memory-DoS guard: keys are attacker-controlled (random usernames), and stale keys were only
# pruned when queried AGAIN — so unique garbage usernames grew the dict without bound. A full
# sweep runs opportunistically once per window, and a hard cap bounds the worst case.
_MAX_FAIL_KEYS = 10_000
_last_sweep = 0.0


def _prune(key: str, now: float) -> list[float]:
    window = settings.login_window_seconds
    arr = [t for t in _fail_log.get(key, []) if now - t < window]
    if arr:
        _fail_log[key] = arr
    else:
        _fail_log.pop(key, None)
    return arr


def _sweep(now: float) -> None:
    """Drop EVERY expired key (not just re-queried ones). Called under _fail_lock."""
    global _last_sweep
    if now - _last_sweep < settings.login_window_seconds:
        return
    _last_sweep = now
    window = settings.login_window_seconds
    for key in list(_fail_log):
        arr = [t for t in _fail_log[key] if now - t < window]
        if arr:
            _fail_log[key] = arr
        else:
            del _fail_log[key]


def login_retry_after(*keys: str) -> int:
    """Seconds to wait before another login attempt is allowed, else 0."""
    import time
    now = time.time()
    wait = 0
    with _fail_lock:
        _sweep(now)
        for key in keys:
            arr = _prune(key, now)
            if len(arr) >= settings.login_max_attempts:
                wait = max(wait, int(settings.login_window_seconds - (now - arr[0])) + 1)
    return wait


def record_login_failure(*keys: str) -> None:
    import time
    now = time.time()
    with _fail_lock:
        _sweep(now)
        for key in keys:
            if key not in _fail_log and len(_fail_log) >= _MAX_FAIL_KEYS:
                # At the cap, drop the oldest-touched key rather than grow unbounded. The
                # throttle degrades gracefully (one stale key forgotten) instead of OOMing.
                oldest = min(_fail_log, key=lambda k: _fail_log[k][-1])
                del _fail_log[oldest]
            _fail_log.setdefault(key, []).append(now)


def clear_login_failures(*keys: str) -> None:
    with _fail_lock:
        for key in keys:
            _fail_log.pop(key, None)


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


def require_permission(permission: str):
    """Dependency factory: 403 unless the caller holds ``permission`` (admins always do). Use on
    the user-facing endpoints (hook / acquire / add / send / page reads) gated by capability."""
    from .permissions import has_permission

    def _dep(user: User = Depends(current_user), db: Session = Depends(get_db)) -> User:
        if not has_permission(db, user, permission):
            raise HTTPException(403, f"You don't have permission to do this ({permission}).")
        return user

    return _dep
