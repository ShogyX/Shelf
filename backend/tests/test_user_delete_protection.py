"""Account hard-delete protection (app/safety.py): a DELETE on the users table is refused on the
production DB unless authorized with SHELF_USER_DELETE_SECRET. Disable/enable stays unguarded, and the
guard is inert on a throwaway/test DB or when no secret is configured."""
from __future__ import annotations

import pytest
from sqlalchemy import delete as sa_delete

from app import safety
from app.db import SessionLocal, init_db
from app.models import User

SECRET = "open-sesame-1234"


@pytest.fixture
def prod_protected(monkeypatch):
    """Simulate production: a non-disposable DB + a configured delete secret → the guard is armed."""
    monkeypatch.setattr(safety, "db_is_disposable", lambda *a, **k: False)
    monkeypatch.setattr(safety.get_settings(), "user_delete_secret", SECRET, raising=False)
    yield


def _user(db, name):
    u = User(username=name, password_hash="x", role="user")
    db.add(u); db.commit(); db.refresh(u)
    return u


def _force_delete(db, u):
    with safety.authorized_user_delete(SECRET):
        db.delete(u); db.commit()


def test_naive_orm_delete_blocked(prod_protected):
    init_db(); db = SessionLocal()
    u = _user(db, "victim-orm")
    with pytest.raises(safety.UserDeleteProtected):
        db.delete(u); db.commit()
    db.rollback()
    assert db.get(User, u.id) is not None            # account survived the blocked delete
    _force_delete(db, u)
    db.close()


def test_bulk_core_delete_blocked(prod_protected):
    init_db(); db = SessionLocal()
    u = _user(db, "victim-bulk")
    with pytest.raises(safety.UserDeleteProtected):
        db.execute(sa_delete(User).where(User.id == u.id)); db.commit()
    db.rollback()
    assert db.get(User, u.id) is not None
    _force_delete(db, u)
    db.close()


def test_authorized_delete_succeeds(prod_protected):
    init_db(); db = SessionLocal()
    u = _user(db, "victim-ok"); uid = u.id
    _force_delete(db, u)
    assert db.get(User, uid) is None
    db.close()


def test_wrong_secret_refused(prod_protected):
    with pytest.raises(safety.UserDeleteProtected):
        with safety.authorized_user_delete("nope"):
            pass
    assert safety.verify_user_delete_secret(SECRET) is True
    assert safety.verify_user_delete_secret("nope") is False
    assert safety.verify_user_delete_secret("") is False


def test_pending_reject_allowed_without_secret(prod_protected):
    """Rejecting a never-approved signup authorizes internally (pending_reject) — no secret needed."""
    init_db(); db = SessionLocal()
    u = _user(db, "pending-signup"); uid = u.id
    u.approval_status = "pending"; db.commit()
    with safety.authorized_user_delete(pending_reject=True):
        db.delete(u); db.commit()
    assert db.get(User, uid) is None
    db.close()


def test_disable_is_not_guarded(prod_protected):
    """Disabling (is_active=false) is an UPDATE, not a DELETE — it must never be gated."""
    init_db(); db = SessionLocal()
    u = _user(db, "victim-disable")
    u.is_active = False; db.commit()                 # must NOT raise
    db.refresh(u)
    assert u.is_active is False
    _force_delete(db, u)
    db.close()


def test_inert_without_secret(monkeypatch):
    """No secret configured → guard inert → delete works (legacy behavior), even on a 'prod' DB."""
    monkeypatch.setattr(safety, "db_is_disposable", lambda *a, **k: False)
    monkeypatch.setattr(safety.get_settings(), "user_delete_secret", "", raising=False)
    init_db(); db = SessionLocal()
    u = _user(db, "victim-nosecret"); uid = u.id
    db.delete(u); db.commit()                        # allowed (protection off)
    assert db.get(User, uid) is None
    db.close()
